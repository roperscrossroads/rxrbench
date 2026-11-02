"""The RAUC flasher against a scripted device: what it refuses, what it calls a success, and
that a device which comes back on its old slot is reported as rolled back."""
import json

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from ansible_collections.rxrbench.linux.plugins.module_utils import rauc

# What a family says of its images: where a bundle lands, and the unit that gates a slot.
LANDS, UNIT = "/data/update.raucb", "boot-health-check.service"

NEW = {"version": "1.0-new", "hash": "aa" * 32}
OLD = {"version": "1.0-old", "hash": "bb" * 32}


def status(booted, a=OLD, b=OLD, boot_status="good"):
    def slot(name, bootname, dev, bundle):
        return {name: {"device": dev, "bootname": bootname,
                       "state": "booted" if booted == bootname else "inactive",
                       "boot_status": boot_status if booted == bootname else "good",
                       "slot_status": {"bundle": {"compatible": "example-pi", **bundle}}}}
    return {"compatible": "example-pi", "booted": booted,
            "slots": [slot("rootfs.0", "A", "/dev/mmcblk0p2", a),
                      slot("rootfs.1", "B", "/dev/mmcblk0p3", b)]}


class FakeTarget:
    """A device on slot A running OLD. `rauc install` fills B; the reboot boots it, unless
    `rolls_back`, in which case the device comes back on A."""

    def __init__(self, rolls_back=False, boot_status="good", free_kb=3_000_000,
                 compatible="example-pi"):
        self.booted, self.slots = "A", {"A": OLD, "B": OLD}
        self.rolls_back, self.boot_status = rolls_back, boot_status
        self.free_kb, self.compatible = free_kb, compatible
        self.commands, self.copied, self.installed = [], [], False
        self.health_props = "LoadState=loaded\nActiveState=active\nResult=success\n"
        self.down = 0

    def run(self, command, timeout=30):  # pylint: disable=unused-argument
        self.commands.append(command)
        if command.startswith("rauc status"):
            s = status(self.booted, self.slots["A"], self.slots["B"], self.boot_status)
            s["compatible"] = self.compatible
            return 0, json.dumps(s)
        if command.startswith("systemctl show"):
            return 0, self.health_props
        if command.startswith("df -k"):
            return 0, f"/dev/mmcblk0p4 3981024 25956 {self.free_kb} 1% /data\n"
        if command.startswith("rauc install"):
            self.slots["B"] = NEW
            self.installed = True
            return 0, "installing\n"
        if "reboot" in command:
            if self.installed and not self.rolls_back:
                self.booted = "B"
            self.down = 2
            return 0, ""
        return 0, ""

    def check(self, command, timeout=30):
        rc, out = self.run(command, timeout)
        assert rc == 0
        return out

    def put(self, local, remote, timeout=600):  # pylint: disable=unused-argument
        self.copied.append((local, remote))

    def up(self):
        if self.down:
            self.down -= 1
            return False
        return True


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(rauc.time, "sleep", lambda s: None)
    p = tmp_path / "image.raucb"
    p.write_bytes(b"\0" * 4096)
    manifest = {"family": "linux_rauc", "format": "raucb", "file": "image.raucb",
                "target": "example-pi", **NEW}
    return str(p), manifest


def test_an_update_fills_the_other_slot_reboots_and_is_judged_by_what_boots(bundle):
    path, manifest = bundle
    t = FakeTarget()
    f = rauc.RaucFlasher(t, manifest, path, LANDS, compatible="example-pi")
    r = common.drive(f, manifest)
    assert r["changed"] and r["after"]["hash"] == NEW["hash"]
    assert t.copied == [(path, "/data/update.raucb")]
    assert f.slot["bootname"] == "B" and f.slot["boot_status"] == "good"
    assert any(c.startswith("rm -f /data/update.raucb") for c in t.commands), "no bundle left behind"


def test_a_device_that_already_runs_the_bundle_is_not_touched(bundle):
    path, manifest = bundle
    t = FakeTarget()
    t.slots["A"] = NEW
    r = common.drive(rauc.RaucFlasher(t, manifest, path, LANDS), manifest)
    assert not r["changed"] and not t.copied
    assert not any("install" in c or "reboot" in c for c in t.commands)


def test_a_plan_copies_nothing(bundle):
    path, manifest = bundle
    t = FakeTarget()
    r = common.drive(rauc.RaucFlasher(t, manifest, path, LANDS), manifest, check_mode=True)
    assert r["changed"] and "version is '1.0-old', the image has '1.0-new'" in r["why"]
    assert not t.copied and not t.installed


def test_a_rollback_is_reported_as_one(bundle):
    path, manifest = bundle
    with pytest.raises(common.FlashError, match="booted slot A again.*rolled back"):
        common.drive(rauc.RaucFlasher(FakeTarget(rolls_back=True), manifest, path, LANDS), manifest)


@pytest.mark.parametrize("target, kw, why", [
    (FakeTarget(compatible="mesh-node-rpi"), {}, "the bundle is for 'example-pi'; the device is 'mesh-node-rpi'"),
    (FakeTarget(), {"compatible": "mesh-node-rpi"}, "the registry says this device is 'mesh-node-rpi'"),
    (FakeTarget(boot_status="bad"), {}, "still proving its last update"),
    (FakeTarget(free_kb=1000), {}, "/data has 0 MiB free"),
])
def test_every_refusal_comes_before_anything_is_copied(bundle, target, kw, why):
    path, manifest = bundle
    with pytest.raises(common.FlashError, match=why):
        common.drive(rauc.RaucFlasher(target, manifest, path, LANDS, **kw), manifest)
    assert not target.copied and not target.installed


def test_a_slot_not_yet_marked_good_is_reported_not_hidden(bundle):
    path, manifest = bundle
    t = FakeTarget()
    f = rauc.RaucFlasher(t, manifest, path, LANDS)
    f.device()                       # the first read is of a good slot ...
    f.preflight()
    f.write()
    t.boot_status = "bad"            # ... the new one is on trial until its health soak passes
    f.verify()
    assert f.slot == {"bootname": "B", "boot_status": "bad", "device": "/dev/mmcblk0p3"}


# ---- the health gate (meta-slotguard's boot-health-check) ----------------------------

def props(load="loaded", active="activating", result="success"):
    return f"LoadState={load}\nActiveState={active}\nResult={result}\n"


@pytest.mark.parametrize("p, want", [
    (props(active="active"), "healthy"),
    (props(active="activating"), "soaking"),
    (props(active="failed", result="exit-code"), "failed"),
    (props(load="not-found", active="inactive"), "unknown"),
])
def test_the_health_gate_is_read_from_its_unit(p, want):
    t = FakeTarget()
    t.health_props = p
    assert rauc.health(t, UNIT) == want


def test_waiting_ends_when_the_gate_passes(monkeypatch):
    t = FakeTarget()
    t.booted = "B"
    states = iter([props(), props(), props(active="active")])
    monkeypatch.setattr(rauc.time, "sleep", lambda s: setattr(t, "health_props", next(states)))
    t.health_props = props()
    slot, state = rauc.wait_healthy(t, "A", seconds=600, unit=UNIT)
    assert state == "healthy" and slot["bootname"] == "B"


def test_a_device_found_back_on_the_slot_it_left_has_rolled_back(monkeypatch):
    t = FakeTarget()
    t.booted, t.health_props = "B", props()

    def crash_loop(_s):           # the new slot keeps resetting; then the bootloader gives up
        t.booted = "A"
    monkeypatch.setattr(rauc.time, "sleep", crash_loop)
    with pytest.raises(common.FlashError, match="back on slot A.*rolled it back"):
        rauc.wait_healthy(t, "A", seconds=600, unit=UNIT)


def test_a_failed_gate_is_an_error_that_says_what_happens_next(monkeypatch):
    monkeypatch.setattr(rauc.time, "sleep", lambda s: None)
    t = FakeTarget()
    t.booted, t.health_props = "B", props(active="failed", result="exit-code")
    with pytest.raises(common.FlashError, match="FAILED its health check.*fail over to slot A"):
        rauc.wait_healthy(t, "A", seconds=600, unit=UNIT)


def test_still_soaking_at_the_deadline_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(rauc.time, "sleep", lambda s: None)
    t = FakeTarget()
    t.booted, t.health_props = "B", props()
    slot, state = rauc.wait_healthy(t, "A", seconds=0, unit=UNIT)
    assert state == "soaking" and slot["bootname"] == "B"


def test_an_image_with_no_gate_is_judged_by_rauc_alone():
    """No unit named: nothing is asked of the device, and a wait ends at once."""
    t = FakeTarget()
    t.booted = "B"
    assert rauc.health(t) == "none"
    assert not any("systemctl" in c for c in t.commands)
    slot, state = rauc.wait_healthy(t, "A", seconds=600)
    assert state == "none" and slot["bootname"] == "B"


def test_a_devices_identity_names_its_own_family():
    t = FakeTarget()
    ident, _slot = rauc.read_device(t, "linux_slotguard")
    assert ident["family"] == "linux_slotguard"


def test_a_bundle_the_device_refuses_is_not_left_on_it(bundle):
    """The device verifies the signature itself. When it refuses, the bundle (the size of a
    root filesystem, on the device's data partition) is removed and nothing is rebooted."""
    from ansible_collections.rxrbench.core.plugins.module_utils import ssh
    path, manifest = bundle

    class Refusing(FakeTarget):
        def check(self, command, timeout=30):
            if command.startswith("rauc install"):
                self.commands.append(command)
                raise ssh.SshError("`rauc install` rc 1: signature verification failed")
            return super().check(command, timeout)

    t = Refusing()
    with pytest.raises(common.FlashError, match="signature verification failed"):
        common.drive(rauc.RaucFlasher(t, manifest, path, LANDS), manifest)
    assert any(c.startswith(f"rm -f {LANDS}") for c in t.commands), "no bundle left behind"
    assert not any("reboot" in c for c in t.commands)
