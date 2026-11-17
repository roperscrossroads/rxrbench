"""The flasher for a Linux device updated with RAUC (A/B root filesystems).

The image is a signed bundle. Writing it is RAUC's business: the bundle is copied to the
device's data partition and `rauc install` writes the inactive slot and makes it the one to
boot next. The device is then rebooted and read again.

What "it worked" means has two levels, and they are kept apart:

1. the device booted the new slot and reports the bundle (immediately after the reboot);
2. the slot is marked good, which the device does itself after its health soak, minutes later.

`verify()` answers the first. The second is the slot's `boot_status`, returned beside the
identity; a caller that needs it waits, or runs again later. If the device comes back on
the old slot, the update was rolled back (the bootloader's attempt counter, or the health
gate), and that is an error that says so.
"""
import json
import os
import time

from ansible_collections.rxrbench.core.plugins.module_utils import identity, ssh
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common

FORMATS = ("raucb",)


def of_status(status, family="linux_rauc"):
    """A Linux device, from `rauc status --detailed --output-format=json`: the booted slot.
    Returns the identity and the slot's state (bootname, boot_status)."""
    booted = [s for entry in status.get("slots", []) for s in entry.values()
              if s.get("state") == "booted"]
    if len(booted) != 1:
        raise ValueError(f"{len(booted)} booted slots in the RAUC status")
    slot = booted[0]
    bundle = (slot.get("slot_status") or {}).get("bundle") or {}
    return (identity.make(family, target=status.get("compatible"), version=bundle.get("version"),
                          hash=bundle.get("hash")),
            {"bootname": slot.get("bootname"), "boot_status": slot.get("boot_status"),
             "device": slot.get("device")})


def of_bundle(info, family="linux_rauc"):
    """A RAUC bundle, from `rauc info --output-format=json`."""
    return identity.make(family, target=info.get("compatible"), version=info.get("version"),
                         hash=info.get("hash"))


def read_device(target, family="linux_rauc"):
    """(identity, slot state) of a device, from `rauc status`."""
    out = target.check("rauc status --detailed --output-format=json", timeout=30)
    try:
        return of_status(json.loads(out[out.index("{"):]), family)
    except (ValueError, KeyError) as e:
        raise common.FlashError(f"cannot read the device's RAUC status: {e}") from e


def health(target, unit=None):
    """Whether the running slot has passed its health gate: `healthy`, `soaking` (the gate
    is still running), `failed`, `unknown` (the device has no such unit), or `none` (its
    family names no gate: RAUC's own `boot_status` is all there is).

    RAUC's `boot_status` does not say this: it reads good as soon as the bootloader has
    attempts left for the slot. On an image with a health gate a unit soaks for some
    minutes, checks the system, and only then lets the slot be marked good. A slot that
    never passes it is not marked good, and the bootloader fails over to the other slot once
    its boot attempts are used up. Which unit that is, is the family's to say.
    """
    if not unit:
        return "none"
    _rc, out = target.run(f"systemctl show -p LoadState,ActiveState,Result {unit}",
                          timeout=20)
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if props.get("LoadState") != "loaded":
        return "unknown"
    if props.get("ActiveState") == "active":
        return "healthy"
    if props.get("ActiveState") == "failed" or props.get("Result") not in (None, "success"):
        return "failed"
    return "soaking"


def wait_healthy(target, left_slot, seconds, sleep=10.0, unit=None, family="linux_rauc"):
    """After an update: wait for the new slot to pass its health gate, for up to `seconds`.
    Returns (slot state, health). Raises FlashError if the device is found running the slot
    it was updated from (it rolled back) or the gate fails.

    The device may reboot while this waits (that is what a rollback looks like from
    outside), so an unreachable device is waited for, not treated as an error.
    """
    end = time.monotonic() + seconds
    slot, state = None, "unknown"
    while True:
        try:
            _ident, slot = read_device(target, family)
            state = health(target, unit)
        except (ssh.SshError, common.FlashError):
            slot = None
        if slot is not None:
            if left_slot and slot["bootname"] == left_slot:
                raise common.FlashError(
                    f"the device is back on slot {left_slot}: the update booted, was not "
                    f"marked good, and the bootloader rolled it back")
            if state == "failed":
                raise common.FlashError(
                    f"the device runs the update on slot {slot['bootname']} but it FAILED "
                    f"its health check: it is not marked good and will fail over to slot "
                    f"{left_slot or 'the other one'} once its boot attempts are used up")
            if state in ("healthy", "unknown", "none"):
                return slot, state
        if time.monotonic() >= end:
            return slot, state
        time.sleep(sleep)


def route_record(delivery, flasher, image, device):
    """What a delivering route says of the device, beside what the device itself runs. The
    device's answer decides whether an update worked; this says whether the route's record
    agrees with it, so a record that does not is seen."""
    out = {"bearer": delivery.bearer, "address": delivery.target}
    if flasher.delivered is not None:
        out["started"] = flasher.delivered
    try:
        says = delivery.state()
    except common.FlashError as e:
        out["route_says"], out["agrees"] = {"error": str(e)}, None
        return out
    out["route_says"] = says
    runs = bool(image.get("hash")) and device.get("hash") == image.get("hash")
    if says.get("open"):
        out["agrees"] = None          # the route has not heard the outcome yet
    elif says.get("result") == "success":
        out["agrees"] = runs
    elif says.get("result") == "failure":
        out["agrees"] = not runs
    else:
        out["agrees"] = None
    return out


class RaucFlasher(common.Flasher):
    """`delivery` (flash/delivery.py) is a route that carries the bundle to the device itself:
    the device is still read here, over ssh, before and after; only the write is the route's.
    `delivery_timeout` is how long the device may take to fetch the bundle, install it and
    reboot (it asks on its own schedule)."""

    def __init__(self, target, manifest, bundle, remote_bundle, compatible=None,
                 reboot_timeout=240.0, delivery=None, delivery_timeout=1800.0, sleep=5.0):
        self.t, self.manifest, self.bundle = target, manifest, bundle
        self.remote_bundle = remote_bundle
        self.family = manifest.get("family") or "linux_rauc"
        self.compatible = compatible
        self.reboot_timeout = reboot_timeout
        self.delivery, self.delivery_timeout, self._sleep = delivery, delivery_timeout, sleep
        #: how long a device whose delivery the route already calls done has to reboot
        self.down_grace = 60.0
        self.delivered = None
        self.before_slot = None
        self.slot = None

    def device(self):
        ident, self.slot = read_device(self.t, self.family)
        return ident

    def preflight(self):
        out = common.common_refusals(self.manifest, FORMATS)
        ident, slot = read_device(self.t, self.family)
        self.before_slot = slot
        if self.manifest.get("target") != ident["target"]:
            out.append(f"the bundle is for {self.manifest.get('target')!r}; the device is "
                       f"{ident['target']!r}")
        if self.compatible is not None and ident["target"] != self.compatible:
            out.append(f"the registry says this device is {self.compatible!r}; it reports "
                       f"{ident['target']!r}")
        if slot["boot_status"] != "good":
            out.append(f"the running slot ({slot['bootname']}) is not marked good "
                       f"({slot['boot_status']}): the device is still proving its last update")
        if self.delivery is not None:
            # The bundle is not put on the device from here: where it lands and whether it
            # fits are the device's own client's. What is asked instead is whether the route
            # can carry it and knows the device, and that no earlier delivery is still open.
            out += self.delivery.refusals(self.manifest)
            if not out:
                known = self.delivery.state()
                if not known.get("known"):
                    out.append(f"its {self.delivery.bearer} route does not know the target "
                               f"{self.delivery.target!r}")
                elif known.get("open"):
                    out.append(f"its {self.delivery.bearer} route still has a delivery in flight "
                               f"for it ({known.get('detail') or 'no detail'})")
            return out
        size = os.path.getsize(self.bundle)
        landing = os.path.dirname(self.remote_bundle) or "/"
        rc, df = self.t.run(f"df -k {landing} | tail -1", timeout=15)
        try:
            free = int(df.split()[3]) * 1024
            if rc != 0 or free < size + (16 << 20):
                out.append(f"{landing} has {free >> 20} MiB free; the bundle needs "
                           f"{(size >> 20) + 16} MiB")
        except (ValueError, IndexError):
            out.append(f"cannot read the free space on {landing}")
        return out

    def _boot(self):
        """The kernel's own id of the running boot, or None while the device does not answer."""
        rc, out = self.t.run("cat /proc/sys/kernel/random/boot_id", timeout=15)
        return (out.strip() or None) if rc == 0 else None

    def _deliver(self):
        """The write, by a route that carries the bundle itself: say which bundle, then wait
        until the device has fetched it, installed it and rebooted. The device asks on its own
        schedule and may be down for less time than one look takes, so a reboot is known by
        the kernel's boot id having changed, not only by the device being seen down.

        A delivery the route closes as failed while the same boot still runs failed on the
        device before any reboot: the route's own word says why. One it closes as done in the
        same boot is a client that reports before its reboot: it is given the time a push
        gives a device to go down, and the slot it then runs is read like any other."""
        boot = self._boot()
        self.delivered = self.delivery.deliver(self.bundle, self.manifest)
        end = time.monotonic() + self.delivery_timeout
        done_at = None
        while True:
            if not self.t.up():
                return
            now = self._boot()
            if boot is not None and now is not None and now != boot:
                return
            state = self.delivery.state()
            if not state.get("open") and state.get("result") == "failure":
                raise common.FlashError(
                    f"the {self.delivery.bearer} delivery failed before the device rebooted: "
                    f"{state.get('detail') or 'no detail'}")
            if not state.get("open") and state.get("result") == "success":
                done_at = done_at or time.monotonic()
                if time.monotonic() - done_at > self.down_grace:
                    return
            if time.monotonic() > end:
                raise common.FlashError(
                    f"the device did not take the {self.delivery.bearer} delivery and reboot "
                    f"within {self.delivery_timeout:.0f} s "
                    f"({state.get('detail') or 'the route has no word of it'})")
            time.sleep(self._sleep)

    def write(self):
        if self.delivery is not None:
            self._deliver()
            return
        try:
            self.t.put(self.bundle, self.remote_bundle)
            self.t.check(f"rauc install {self.remote_bundle}", timeout=900)
        except ssh.SshError as e:
            raise common.FlashError(str(e)) from e
        finally:
            # Installed or refused, the bundle does not stay on the device: it is the size
            # of a root filesystem, on the partition the device's own data lives on.
            self.t.run(f"rm -f {self.remote_bundle}", timeout=30)
        # The reboot takes the connection with it: its exit status says nothing.
        self.t.run("sync; (sleep 1; reboot) >/dev/null 2>&1 &", timeout=15)
        end = time.monotonic() + 60
        while self.t.up():
            if time.monotonic() > end:
                raise common.FlashError("the device did not go down for its reboot")
            time.sleep(2)

    def verify(self):
        end = time.monotonic() + self.reboot_timeout
        while not self.t.up():
            if time.monotonic() > end:
                raise common.FlashError(f"the device did not come back within "
                                        f"{self.reboot_timeout:.0f} s of the reboot")
            time.sleep(3)
        ident, self.slot = read_device(self.t, self.family)
        if self.before_slot and self.slot["bootname"] == self.before_slot["bootname"]:
            raise common.FlashError(
                f"the device booted slot {self.slot['bootname']} again: the update was "
                f"installed but not booted, or it was rolled back")
        return ident
