#!/usr/bin/env python3
"""devreg — named hardware registry for embedded targets.

Resolve boards by NAME instead of by /dev/sdX or a usbipd busid, so that
concurrent agent sessions cannot write to each other's hardware (or to the
workstation's own disks).

Registry: $DEVREG_REGISTRY, else the rxrbench site's (see _find_registry).

Exit codes are meaningful — callers should check them:
    0  ok
    1  not found / not present
    2  ambiguous (more than one device matched)
    3  refused (protected path, or guard mismatch)
    4  registry / usage error
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("devreg: needs pyyaml (pip install pyyaml)", file=sys.stderr)
    sys.exit(4)

# devlock lives next to this file. Imported by path rather than by package so
# devreg keeps working however it is invoked (`just devreg`, ~/.devreg/devreg.py on a bench
# host, a wrapper of the user's own), and cwd is never assumed.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import devlock  # noqa: E402

def _find_registry() -> Path:
    """Where the registry lives, in order of preference.

    1. DEVREG_REGISTRY, for a session testing a candidate registry, or a site that keeps it
       elsewhere;
    2. a registry.yaml next to this script: the self-contained copy `devreg deploy` (and
       rxrbench's `just hosts-setup`) puts on a bench host that has no checkout;
    3. the rxrbench site this script belongs to (its .env, else <site>/registry.yaml), when it
       runs from the repository.
    """
    env = os.environ.get("DEVREG_REGISTRY")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent
    beside = here / "registry.yaml"
    if beside.exists():
        return beside
    collections = Path(os.environ.get("RXRBENCH_COLLECTIONS") or here.parent / "collections")
    if collections.is_dir():
        sys.path.insert(0, str(collections))
        try:
            from ansible_collections.rxrbench.core.plugins.module_utils import site
            site.load_env()
            return Path(site.registry_path())
        except ImportError:
            pass
    return beside  # so the error message names a location


REGISTRY = _find_registry()

OK, NOTFOUND, AMBIGUOUS, REFUSED, USAGE = 0, 1, 2, 3, 4

# The registry contract (docs/REGISTRY.md): the keys this tool and rxrbench read, per block. A
# site may carry any other key; `devreg lint` names them so a site knows what is its own.
CONTRACT = {
    "file": {"version", "hosts", "devices", "protected"},
    "host": {"ssh", "local", "hostname", "port", "ssh_key", "known_hosts", "protected",
             "description", "bluetooth", "usbipd"},
    "device": {"kind", "aliases", "match", "match_also", "firmware", "destructive", "bootloader",
               "reach", "policy", "sim_exe", "sim_seed", "sim_console",
               # written by `enroll`, read by the site: part of the shape, not of the reads
               "description", "owner", "hardware", "identity"},
    "match": {"usb_vid", "usb_pid", "usb_serial", "serial_by_id", "block_by_id", "host",
              "sibling_vid", "sibling_pid", "sibling_serial", "sim_device_id"},
    "reach": {"host", "hosts", "state", "can", "addr", "ip", "port", "user", "jump", "identity",
              "known_hosts", "probe", "chip", "uid", "nrst", "node", "tool", "modes", "confirmed",
              "note"},
    "mode": {"by_id", "usb", "can", "enter", "note"},
}
BEARERS = ("usb", "ssh", "swd", "ble", "wifi", "eth", "lora")


# ── registry ────────────────────────────────────────────────────────────────

def load_registry() -> dict:
    if not REGISTRY.exists():
        print(f"devreg: no registry at {REGISTRY}", file=sys.stderr)
        sys.exit(USAGE)
    with REGISTRY.open() as fh:
        data = yaml.safe_load(fh) or {}
    data.setdefault("devices", {})
    data.setdefault("protected", [])
    # A null entry (all-commented template) is not a device.
    data["devices"] = {k: v for k, v in data["devices"].items() if v}
    return data


# ── hosts ───────────────────────────────────────────────────────────────────
#
# A device's identity is its serial, not the host it is plugged into. These helpers
# answer "which host has it": the local machine, or each registry host over ssh.

def hosts(reg: dict) -> dict:
    return reg.get("hosts") or {}


def local_host(reg: dict) -> str | None:
    """The registry key of the machine this runs on, if the registry declares it.

    The hostname is matched first (the key, or a host's `hostname:`); the `local:` flag is the
    fallback. The flag belongs to the file, which is copied to every bench host, so on its own
    it would name the same host everywhere. It exists for a host whose hostname is not its
    registry key (a WSL workstation).
    """
    import socket
    hn = socket.gethostname().split(".")[0].lower()
    for name, h in hosts(reg).items():
        if name.lower() == hn or str((h or {}).get("hostname", "")).lower() == hn:
            return name
    for name, h in hosts(reg).items():
        if (h or {}).get("local"):
            return name
    return None


def _probe() -> dict:
    """Enumerate this machine's USB, serial and block devices.

    Its source is sent verbatim to remote hosts (RemoteProber) and run there by a bare
    `python3`: stdlib only, imports inside the body, nothing else from this file. The local
    and remote probe are the same code, so both report a device the same way.
    """
    import os

    def usb_devices():
        # sysfs rather than lsusb because it carries the serial number, which is
        # the only thing that distinguishes two boards of the same model.
        out = []
        root = "/sys/bus/usb/devices"
        if not os.path.isdir(root):
            return out
        for name in sorted(os.listdir(root)):
            dev = os.path.join(root, name)
            vid = os.path.join(dev, "idVendor")
            pid = os.path.join(dev, "idProduct")
            if not (os.path.exists(vid) and os.path.exists(pid)):
                continue  # interfaces and root hubs
            def rd(p):
                try:
                    with open(p) as fh:
                        return fh.read().strip()
                except OSError:
                    return None
            entry = {
                "usb_vid": (rd(vid) or "").lower(),
                "usb_pid": (rd(pid) or "").lower(),
                # realpath, not the /sys/bus/usb/devices/<busid> symlink: sysfs
                # walks from a /dev node land on /sys/devices/..., and the two
                # spellings of one device must compare equal.
                "syspath": os.path.realpath(dev),
                "busid": name,
            }
            for opt in ("serial", "product", "manufacturer", "bDeviceClass"):
                v = rd(os.path.join(dev, opt))
                if v is not None:
                    entry[opt] = v
            out.append(entry)
        return out

    def owning_usb(devnode):
        """Walk sysfs from a /dev node up to the USB device that provides it."""
        base = os.path.basename(os.path.realpath(devnode))
        if not base:
            return None
        for p in (f"/sys/class/tty/{base}", f"/sys/block/{base}",
                  f"/sys/class/block/{base}"):
            if not os.path.exists(p):
                continue
            start = os.path.join(p, "device")
            node = os.path.realpath(start if os.path.exists(start) else p)
            # The USB device is the first ancestor carrying idVendor; the nodes
            # below it are interfaces (1-1:1.0) and transport layers (host0/...).
            while node and node != "/":
                if os.path.exists(os.path.join(node, "idVendor")):
                    return node
                node = os.path.dirname(node)
        return None

    def by_id(dirpath):
        if not os.path.isdir(dirpath):
            return []
        out = []
        for name in sorted(os.listdir(dirpath)):
            link = os.path.join(dirpath, name)
            try:
                path = os.path.realpath(link)
            except OSError:
                continue
            # syspath is resolved at enumeration time: a remote prober cannot walk the
            # remote sysfs afterwards.
            out.append({"id": name, "path": path, "syspath": owning_usb(path)})
        return out

    def serial_ports():
        # by-id names, then any USB tty that has none: two serial-less bridges of one make
        # (two CH340s) want the same by-id name, udev gives it to one, and the other's tty
        # would be no port at all (its board resolved to a bare VID:PID, which no writer takes).
        out = by_id("/dev/serial/by-id")
        named = {d["path"] for d in out}
        root = "/sys/class/tty"
        for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            path = "/dev/" + name
            if path in named or not os.path.exists(os.path.join(root, name, "device")):
                continue
            usb = owning_usb(path)
            if usb:
                out.append({"id": "", "path": path, "syspath": usb})
        return out

    return {
        "usb": usb_devices(),
        "serial": serial_ports(),
        "block": by_id("/dev/disk/by-id"),
    }


class Prober:
    """Where devreg looks for hardware. One per host, results cached per run."""

    def __init__(self, name: str):
        self.name = name
        self._d: dict | None = None
        self.error: str | None = None

    def data(self) -> dict:
        if self._d is None:
            self._d = self._load()
        return self._d

    def _load(self) -> dict:
        raise NotImplementedError

    def usb(self):
        return self.data().get("usb", [])

    def serial(self):
        return self.data().get("serial", [])

    def block(self):
        return self.data().get("block", [])


class LocalProber(Prober):
    def _load(self) -> dict:
        return _probe()


class RemoteProber(Prober):
    """Runs _probe() on another host over ssh and reads back JSON.

    It does not need devreg installed on that host, only `python3` and ssh access, so a new
    host can be probed before anything is deployed to it.
    """

    def __init__(self, name: str, ssh: str):
        super().__init__(name)
        self.ssh = ssh

    def _load(self) -> dict:
        import inspect
        import json
        import subprocess
        driver = (inspect.getsource(_probe)
                  + "\nimport json, sys\nsys.stdout.write(json.dumps(_probe()))\n")
        try:
            # The timeout runs inside the remote command: a local timeout kills only the
            # ssh client and leaves the remote process running (docs/HARDWARE.md,
            # "Bluetooth LE and MCUmgr"). The local timeout is a longer backstop for a hung
            # TCP handshake.
            p = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 self.ssh, "timeout 20 python3 -"],
                input=driver, capture_output=True, text=True, timeout=40)
        except (subprocess.TimeoutExpired, OSError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return {}
        if p.returncode != 0:
            self.error = (p.stderr.strip().splitlines() or ["ssh failed"])[-1]
            return {}
        try:
            return json.loads(p.stdout)
        except ValueError as exc:
            self.error = f"unparseable probe output: {exc}"
            return {}


class UnreachableProber(Prober):
    """A host that cannot be probed from here: not this machine, and no `ssh:` entry.

    A host without `ssh:` is not assumed to be local: probing this machine and labelling the
    answer with another host's name would report that host's boards where they are not.
    """

    def __init__(self, name: str, why: str):
        super().__init__(name)
        self._why = why

    def _load(self) -> dict:
        self.error = self._why
        return {}


def prober_for(reg: dict, host: str | None) -> Prober:
    """A prober for `host`, or for this machine if host is None."""
    if host is None:
        return LocalProber(local_host(reg) or "local")
    h = hosts(reg).get(host)
    if h is None:
        print(f"devreg: unknown host '{host}' "
              f"(known: {', '.join(sorted(hosts(reg))) or 'none'})", file=sys.stderr)
        sys.exit(USAGE)
    if host == local_host(reg):
        return LocalProber(host)
    if not (h or {}).get("ssh"):
        return UnreachableProber(
            host, "no `ssh:` in its registry entry, and it is not this machine")
    return RemoteProber(host, h["ssh"])


def all_probers(reg: dict) -> list[Prober]:
    """Every host that can be probed, this machine first."""
    lh = local_host(reg)
    out = [prober_for(reg, lh)] if lh else [LocalProber("local")]
    for name in sorted(hosts(reg)):
        if name != lh:
            out.append(prober_for(reg, name))
    return out


# ── discovery (local shims, kept so existing call sites are unchanged) ───────

def present_usb() -> list[dict]:
    return _probe()["usb"]


def present_block() -> list[dict]:
    return _probe()["block"]


def present_serial() -> list[dict]:
    return _probe()["serial"]


def usb_syspath_for_devnode(devnode: str) -> str | None:
    """Walk sysfs from a /dev node up to the USB device that provides it.

    A board registered by serial_by_id/block_by_id resolves to /dev/ttyACM0 or
    /dev/sdX, while `scan` enumerates USB syspaths. This mapping lines the two up, so `scan`
    does not report a registered board as unregistered (and invite a second entry for it).
    """
    name = os.path.basename(os.path.realpath(devnode))
    if not name:
        return None
    for base in (f"/sys/class/tty/{name}", f"/sys/block/{name}", f"/sys/class/block/{name}"):
        p = Path(base)
        if not p.exists():
            continue
        start = p / "device"
        try:
            node = (start if start.exists() else p).resolve()
        except OSError:
            continue
        # The USB device is the first ancestor carrying idVendor; the nodes
        # below it are interfaces (1-1:1.0) and transport layers (host0/...).
        for cand in (node, *node.parents):
            if (cand / "idVendor").exists():
                return str(cand)
    return None


# ── matching ────────────────────────────────────────────────────────────────

# The keys that tell one device from another of the same make. A match block with none of
# them (VID:PID alone: a CH340 has no serial) matches every device of that make.
DISTINGUISHING = ("usb_serial", "serial_by_id", "block_by_id",
                  "sibling_vid", "sibling_pid", "sibling_serial")


def _blocks(spec: dict) -> list[dict]:
    # A board may have more than one USB face: a stock app on native USB (TinyUSB CDC, the
    # MAC as a bare serial) and the ROM / USB-Serial-JTAG face (the MAC with colons), say.
    # `match` is the lasting face, `match_also` the others; a hit on any is the board.
    return [spec.get("match") or {}] + list(spec.get("match_also") or [])


def _distinguishes(block: dict) -> bool:
    return any(k in (block or {}) for k in DISTINGUISHING)


def _hit_id(h: dict) -> str:
    return h.get("syspath") or h["identifier"]


def match_device(spec: dict, prober: Prober | None = None,
                 claimed: set[str] | frozenset = frozenset()) -> list[dict]:
    """Everything present that satisfies a device's match block.

    `prober` says which host "present" means; None means this machine. `claimed` holds the
    devices another entry names by a distinguishing key (`claimed_by_others`): a block that
    matches by make alone does not count them.
    """
    pr = prober or LocalProber("local")
    hits: list[dict] = []
    seen_ids: set[str] = set()
    for blk in _blocks(spec):
        by_make = not _distinguishes(blk)
        for h in _match_block(spec, blk, pr):
            ident = _hit_id(h)
            if by_make and ident in claimed:
                continue
            if ident not in seen_ids:
                seen_ids.add(ident)
                hits.append(h)
    return hits


def claimed_by_others(reg: dict, name: str, pr: Prober) -> set[str]:
    """The devices present that an entry other than `name` names by a distinguishing key.

    Two boards behind serial-less CH340s, one of them registered beside its own RP2040
    (`sibling_*`) and the other by VID:PID alone: the first bridge is the first board, so the
    second entry does not count it, and it resolves to its own bridge instead of refusing
    both as ambiguous (or, with its own board unplugged, taking the other board for itself).
    Only a distinguishing match claims: two entries both by make alone stay ambiguous.
    """
    out: set[str] = set()
    for other, spec in reg["devices"].items():
        if other == name:
            continue
        for blk in _blocks(spec):
            if _distinguishes(blk):
                out.update(_hit_id(h) for h in _match_block(spec, blk, pr))
    return out


def match_in_registry(reg: dict, name: str, pr: Prober) -> list[dict]:
    """`match_device` for a registry entry, less what other entries claim (`claimed_by_others`).
    Only an entry with a block that matches by make alone needs the rest of the registry."""
    dev = reg["devices"][name]
    if all(_distinguishes(b) for b in _blocks(dev) if b):
        return match_device(dev, pr)
    return match_device(dev, pr, claimed_by_others(reg, name, pr))


def _match_block(spec: dict, block: dict, pr: Prober) -> list[dict]:
    m = {k: str(v).lower() for k, v in (block or {}).items()}
    if not m:
        return []
    # `host:` pins a device to one bench host. Two boards can be identical at the USB level
    # (docs/HARDWARE.md, "USB identity and hubs"), and without the pin one entry also
    # matches the other board on another host. A prober that does not know which host it is
    # ("local") cannot apply the pin and lets it pass.
    want_host = m.pop("host", None)
    if want_host and pr.name.lower() not in ("local", want_host):
        return []
    hits: list[dict] = []
    want_kind = str(spec.get("kind") or "").lower()

    if "usb_vid" in m or "usb_pid" in m or "usb_serial" in m:
        for d in pr.usb():
            if "usb_vid" in m and d["usb_vid"] != m["usb_vid"]:
                continue
            if "usb_pid" in m and d["usb_pid"] != m["usb_pid"]:
                continue
            if "usb_serial" in m and d.get("serial", "").lower() != m["usb_serial"]:
                continue
            if not _has_sibling(pr, d, m):
                continue
            # A `kind: serial`/`block` device matched at the USB level (by usb_serial, the
            # chip id that survives every firmware and bootloader mode) still needs a /dev
            # node: resolve, guard and flash consume a path, and a VID:PID guards nothing.
            # The matched USB device maps to the devnode(s) it provides now. With none (a
            # mass-storage-only mode) the bare USB hit stands, so `list` shows the board present.
            if want_kind in ("serial", "block"):
                finder = pr.serial if want_kind == "serial" else pr.block
                mapped = False
                for n in finder():
                    # syspath is resolved by the prober at enumeration time, so
                    # this comparison works identically for a remote host.
                    if n.get("syspath") == d["syspath"]:
                        hits.append({**d, **n, "kind": want_kind, "identifier": n["path"]})
                        mapped = True
                if mapped:
                    continue
            hits.append({"kind": "usb", "identifier": f"{d['usb_vid']}:{d['usb_pid']}", **d})

    for key, finder, kind in (
        ("serial_by_id", pr.serial, "serial"),
        ("block_by_id", pr.block, "block"),
    ):
        if key in m:
            for d in finder():
                if d["id"].lower() == m[key]:
                    hits.append({"kind": kind, "identifier": d["path"], **d})

    return hits


def _has_sibling(pr, d: dict, m: dict) -> bool:
    """True unless `m` names a sibling (`sibling_vid`/`sibling_pid`/`sibling_serial`) and no
    other USB device on `d`'s parent hub carries those ids.

    For a USB bridge with no serial number (a CH340: every generic adapter reports 1a86:7523)
    on a board with a second, serial-bearing chip behind the same on-board hub: the bridge
    counts as the board only beside that chip, and a CH340 anywhere else matches nothing.
    """
    want = {k[len("sibling_"):]: v for k, v in m.items() if k.startswith("sibling_")}
    if not want:
        return True
    hub = os.path.dirname(d["syspath"])
    for s in pr.usb():
        if s["syspath"] == d["syspath"] or os.path.dirname(s["syspath"]) != hub:
            continue
        have = {"vid": s["usb_vid"], "pid": s["usb_pid"], "serial": s.get("serial", "").lower()}
        if all(have[k] == v for k, v in want.items()):
            return True
    return False


def canonical_name(reg: dict, name: str) -> str | None:
    """Registry key for `name`, following the `aliases:` list. Every command that takes a
    device name resolves it through here, so a declared alias works wherever the name does."""
    if name in reg["devices"]:
        return name
    for key, dev in reg["devices"].items():
        if name in (dev.get("aliases") or ()):
            return key
    return None


def resolve_one(reg: dict, name: str,
                prober: Prober | None = None) -> tuple[int, dict | None, str]:
    key = canonical_name(reg, name)
    dev = reg["devices"].get(key) if key else None
    if dev is None:
        known = ", ".join(
            n + (f" ({', '.join(d['aliases'])})" if d.get("aliases") else "")
            for n, d in sorted(reg["devices"].items())
        ) or "(registry empty)"
        return USAGE, None, f"unknown device '{name}'. registered: {known}"
    name = key

    hits = match_in_registry(reg, name, prober or LocalProber("local"))
    if not hits:
        where = f" on {prober.name}" if prober else ""
        hint = ""
        # "Absent" means absent from USB on this host. A board can be alive on BLE or LoRa
        # with no USB at all, and on a usbipd host it appears only once attached.
        if (dev.get("reach") or {}).get("ble") or (dev.get("reach") or {}).get("lora"):
            hint = " — but it has non-USB routes; try `devreg reach %s`" % name
        return NOTFOUND, None, (
            f"'{name}' is registered but NOT PRESENT{where} right now{hint}")
    if len(hits) > 1:
        detail = "\n".join(
            f"    - {h.get('busid', h.get('id', '?'))}  {h['identifier']}"
            f"  serial={h.get('serial', '(none)')}"
            for h in hits
        )
        return AMBIGUOUS, None, (
            f"'{name}' matched {len(hits)} devices — REFUSING to guess.\n{detail}\n"
            "  Add a distinguishing key (usb_serial / serial_by_id) to the registry,\n"
            "  or unplug the others."
        )
    return OK, hits[0], ""


# ── commands ────────────────────────────────────────────────────────────────

def cmd_list(reg: dict, args) -> int:
    if not reg["devices"]:
        print("(no devices registered)")
        return OK
    pr = prober_for(reg, getattr(args, "host", None))
    if getattr(args, "host", None):
        print(f"(on {pr.name})")
    for name, dev in sorted(reg["devices"].items()):
        rc, hit, _ = resolve_one(reg, name, pr)
        state = {
            OK: "PRESENT",
            NOTFOUND: "absent",
            AMBIGUOUS: "AMBIGUOUS!",
        }.get(rc, "?")
        flag = " [destructive]" if dev.get("destructive") else ""
        print(f"{name:20s} {state:11s} {dev.get('description', '')}{flag}")
        if hit:
            print(f"{'':20s}   -> {hit['identifier']}  ({hit.get('busid', hit.get('id', ''))})")
    if pr.error:
        print(f"\ndevreg: could not probe {pr.name}: {pr.error}", file=sys.stderr)
        return NOTFOUND
    return OK


def cmd_where(reg: dict, args) -> int:
    """Which host has this device now, probing every registry host.

    `list` and `resolve` answer for one host; `where` answers across all of them.
    """
    names = [args.name] if args.name else sorted(reg["devices"])
    if args.name:
        key = canonical_name(reg, args.name)
        if key is None:
            print(f"devreg: unknown device '{args.name}'", file=sys.stderr)
            return USAGE
        names = [key]

    probers = all_probers(reg)
    found_any = False
    for name in names:
        dev = reg["devices"][name]
        located = []
        for pr in probers:
            rc, hit, _ = resolve_one(reg, name, pr)
            if rc == OK:
                located.append((pr.name, hit["identifier"]))
            elif rc == AMBIGUOUS:
                located.append((pr.name, "AMBIGUOUS — refusing to guess"))
        if located:
            found_any = True
            for host, ident in located:
                print(f"{name:20s} {host:10s} {ident}")
            if len(located) > 1:
                print(f"{'':20s} {'':10s} warning: matched on more than one host: one "
                      f"registry entry, two physical boards?")
        elif args.name:
            # Only worth saying for a specific query; in a full sweep the absent
            # majority is noise.
            claimed = ((dev.get("reach") or {}).get("usb") or {}).get("host")
            extra = f" (registry says its USB lives on: {claimed})" if claimed else ""
            print(f"{name:20s} {'—':10s} not on USB on any reachable host{extra}")
            reach = dev.get("reach") or {}
            for bearer in ("ble", "lora", "wifi", "eth"):
                b = reach.get(bearer)
                if not b:
                    continue
                state = b.get("state", "?")
                tgt = b.get("addr") or b.get("node") or b.get("ip") or ""
                print(f"{'':20s} {bearer:10s} {tgt}  [{state}]")

    for pr in probers:
        if pr.error:
            print(f"devreg: could not probe {pr.name}: {pr.error}", file=sys.stderr)
    return OK if found_any or not args.name else NOTFOUND


def cmd_reach(reg: dict, args) -> int:
    """Every recorded way to get at a device, and what each one can do.

    It does not probe: it prints what the registry records, dormant and unproven routes
    included. Probing every bearer would key radios and connect Bluetooth.
    """
    key = canonical_name(reg, args.name)
    if key is None:
        print(f"devreg: unknown device '{args.name}'", file=sys.stderr)
        return USAGE
    dev = reg["devices"][key]
    reach = dev.get("reach") or {}
    if not reach:
        print(f"devreg: '{key}' has no reach block "
              f"(only `match`, which is identity, not reachability)", file=sys.stderr)
        return NOTFOUND

    print(f"{key} — {dev.get('description', '')}")
    for bearer, b in reach.items():
        b = b or {}
        bits = []
        for k in ("host", "hosts", "addr", "ip", "node", "state", "confirmed", "tool"):
            if b.get(k) is not None:
                bits.append(f"{k}={b[k]}")
        can = b.get("can")
        print(f"  {bearer:<6} {' '.join(bits)}")
        if can:
            print(f"  {'':<6}   can: {', '.join(can)}")
        if args.verbose and b.get("note"):
            print(f"  {'':<6}   note: {' '.join(str(b['note']).split())}")
        for mode, m in (b.get("modes") or {}).items():
            m = m or {}
            desc = m.get("by_id") or m.get("usb") or ""
            mc = f"  can: {', '.join(m['can'])}" if m.get("can") else ""
            print(f"  {'':<6}   [{mode}] {desc}{mc}")
            if m.get("enter"):
                print(f"  {'':<6}       enter: {m['enter']}")
            if args.verbose and m.get("note"):
                print(f"  {'':<6}       note: {' '.join(str(m['note']).split())}")
    if not args.verbose:
        print("  (-v also prints the per-bearer/per-mode notes — several are traps)")
    return OK


def cmd_scan(reg: dict, args) -> int:
    """Present hardware that is not registered: candidates to add."""
    pr = prober_for(reg, getattr(args, "host", None))
    if getattr(args, "host", None):
        print(f"(on {pr.name})")
    # syspath/identifier -> registered name. A device matched by serial_by_id or block_by_id
    # has no syspath of its own, so its /dev node maps back to the owning USB device; without
    # that it would be listed here as unregistered while `list` shows it present.
    registered: dict[str, str] = {}
    for name, dev in reg["devices"].items():
        for h in match_device(dev, pr):
            if h.get("syspath"):
                registered[h["syspath"]] = name
            registered[h["identifier"]] = name

    if pr.error:
        print(f"devreg: could not probe {pr.name}: {pr.error}", file=sys.stderr)
        return NOTFOUND

    print("=== USB devices present but UNREGISTERED ===")
    found = False
    for d in pr.usb():
        if d["syspath"] in registered:
            continue
        # Hubs are left out: root hubs by vendor (1d6b), external ones by bDeviceClass 09 (a
        # tiered hub adds one entry per tier). `--hubs` lists them, for when the hub itself
        # is the problem.
        is_hub = d["usb_vid"] == "1d6b" or d.get("bDeviceClass") == "09"
        if is_hub and not getattr(args, "hubs", False):
            continue
        found = True
        print(f"  {d['busid']:12s} {d['usb_vid']}:{d['usb_pid']}  "
              f"{d.get('product', '(no product string)')}")
        print(f"    serial: {d.get('serial', '(none)')}")
        print( "    suggested registry entry:")
        print( "      <name>:")
        print(f"        description: {d.get('product', 'TODO')}")
        print( "        kind: usb")
        print( "        destructive: true")
        print( "        match:")
        print(f"          usb_vid: \"{d['usb_vid']}\"")
        print(f"          usb_pid: \"{d['usb_pid']}\"")
        if d.get("serial"):
            print(f"          usb_serial: \"{d['serial']}\"   # keeps two identical boards apart")
    if not found:
        print("  (none)")

    ser = pr.serial()
    if ser:
        print("\n=== serial devices (/dev/serial/by-id, then USB ttys with none) ===")
        for d in ser:
            owner = registered.get(d["path"])
            tag = f"  [registered as '{owner}']" if owner else "  [UNREGISTERED]"
            print(f"  {d['id'] or '(no by-id name: another device has it)'}{tag}\n    -> {d['path']}")
    return OK


def route_address(reg: dict, name: str, bearer: str) -> tuple[int, str | None, str]:
    """The address the registry records for one non-USB route to a device.

    A lookup, not a probe: nothing is connected to and no radio is keyed. It is what a tool
    compares the address it is about to use against, the way a path is compared for USB.
    """
    key = canonical_name(reg, name)
    if key is None:
        return USAGE, None, f"unknown device '{name}'"
    reach = reg["devices"][key].get("reach") or {}
    if bearer not in reach:
        have = ", ".join(reach) or "none"
        return NOTFOUND, None, f"'{key}' has no {bearer} route in the registry (routes: {have})"
    r = reach[bearer] or {}
    # One route, one address. A route that records two different ones is ambiguous, and
    # ambiguity is an error here as it is for two boards matching one name.
    found = {str(r[k]).strip().lower(): str(r[k]).strip() for k in ("addr", "ip")
             if r.get(k) not in (None, "")}
    if not found:
        return NOTFOUND, None, f"'{key}' has a {bearer} route but no address for it"
    if len(found) > 1:
        return AMBIGUOUS, None, (f"'{key}' records {len(found)} addresses for its {bearer} "
                                 f"route ({', '.join(found.values())}): REFUSING to guess")
    return OK, next(iter(found.values())), ""


USB_LOCATION = re.compile(r"^\d+-\d+(\.\d+)*$")


def busid_of(hit: dict) -> str | None:
    """Where on the USB bus a resolved device is (`1-1.2.4`, the sysfs name), or None for one
    with no USB device behind it. openocd takes this as `adapter usb location`."""
    b = str(hit.get("busid") or "")
    return b if USB_LOCATION.match(b) else None


def cmd_resolve(reg: dict, args) -> int:
    if getattr(args, "route", None):
        rc, addr, err = route_address(reg, args.name, args.route)
        if rc != OK:
            print(f"devreg: {err}", file=sys.stderr)
            return rc
        print(addr)
        return OK
    pr = prober_for(reg, getattr(args, "host", None))
    rc, hit, err = resolve_one(reg, args.name, pr)
    if rc != OK:
        print(f"devreg: {err}", file=sys.stderr)
        if pr.error:
            print(f"devreg: could not probe {pr.name}: {pr.error}", file=sys.stderr)
        return rc
    # The path goes to stdout alone, so it can be interpolated into a remote command. A path
    # resolved on another machine looks like a local one, so stderr says where it is (outside
    # any $(...) capture).
    if isinstance(pr, RemoteProber):
        print(f"devreg: note — {hit['identifier']} is a path on {pr.name}, "
              f"not on this machine", file=sys.stderr)
    if getattr(args, "busid", False):
        where = busid_of(hit)
        if where is None:
            print(f"devreg: '{args.name}' has no USB location to give", file=sys.stderr)
            return NOTFOUND
        print(where)
        return OK
    print(hit["identifier"])
    return OK


def protecting_entry(target: str, protected: list[str]) -> str | None:
    """The protected path covering `target`, if any.

    A partition inherits its disk's protection: writing /dev/sda1 destroys the
    protected /dev/sda just as surely. Suffix forms covered are sda->sda1 and
    nvme0n1->nvme0n1p1; /dev/sdaa is a different disk, not a partition.
    """
    for p in protected:
        if target == p:
            return p
        rest = target[len(p):] if target.startswith(p) else ""
        if rest and rest.lstrip("p").isdigit():
            return p
    return None


def protected_paths(reg: dict) -> list[str]:
    """The global protected list, plus anything this host's entry protects.

    Block-device names mean a different disk on each machine (docs/HARDWARE.md, "USB
    identity and hubs"), so each host lists its own. The lists are joined, never replaced: an
    extra entry refuses a write, which is recoverable; a missing one is not.
    """
    out = list(reg.get("protected") or [])
    lh = local_host(reg)
    if lh:
        out += list((hosts(reg).get(lh) or {}).get("protected") or [])
    return out


def cmd_guard(reg: dict, args) -> int:
    """Assert that <path> really is <name>. Call before any destructive write.

    Always local: a guard is the last check before a write, and the write happens on the
    machine holding the device. `--host` is not accepted: a remote path asserted safe here
    would be acted on locally.
    """
    if getattr(args, "route", None):
        # A route that is not a path: the address is the registry's or it is refused. No
        # device is asked anything, so this is as safe to run before a plan as before a write.
        rc, addr, err = route_address(reg, args.name, args.route)
        if rc != OK:
            print(f"devreg: {err}", file=sys.stderr)
            return rc
        if addr.lower() != args.path.strip().lower():
            print(f"devreg: REFUSED — '{args.name}' is at {addr} over {args.route} "
                  f"in the registry, not {args.path}", file=sys.stderr)
            return REFUSED
        print(f"ok: {args.path} is '{args.name}' over {args.route}")
        return OK
    target = os.path.realpath(args.path)

    protected = [os.path.realpath(p) for p in protected_paths(reg)]
    hit_p = protecting_entry(target, protected)
    if hit_p:
        via = "" if target == hit_p else f" (partition of protected {hit_p})"
        print(f"devreg: REFUSED — {args.path} is on the protected list{via}",
              file=sys.stderr)
        return REFUSED

    # Never write to something with a mounted filesystem on it.
    try:
        mounts = Path("/proc/mounts").read_text()
        for line in mounts.splitlines():
            src = line.split()[0]
            if src.startswith("/dev/") and os.path.realpath(src).startswith(target):
                print(f"devreg: REFUSED — {target} has a mounted filesystem ({src})",
                      file=sys.stderr)
                return REFUSED
    except OSError:
        pass

    rc, hit, err = resolve_one(reg, args.name)
    if rc != OK:
        print(f"devreg: {err}", file=sys.stderr)
        return rc

    if os.path.realpath(hit["identifier"]) != target:
        print(f"devreg: REFUSED — '{args.name}' resolves to {hit['identifier']}, "
              f"not {args.path}", file=sys.stderr)
        return REFUSED

    print(f"ok: {args.path} is '{args.name}'")
    return OK


def whois_path(reg: dict, path: str) -> tuple[int, str | None, dict | None, str]:
    """Which registered device is at @path? The inverse of resolve.

    `guard` asserts that a path is a named board, which needs the name; this answers from
    the path, so a caller holding only a port can learn which board it is and branch on it.
    """
    target = os.path.realpath(path)
    matches: list[tuple[str, dict]] = []
    pr = prober_for(reg, None)
    for name, dev in reg["devices"].items():
        for h in match_in_registry(reg, name, pr):
            if os.path.realpath(h["identifier"]) == target:
                matches.append((name, dev))
                break
    if not matches:
        return NOTFOUND, None, None, (
            f"no registered device is at {path}. Run 'devreg scan' if this is new hardware.")
    if len(matches) > 1:
        names = ", ".join(n for n, _ in matches)
        return AMBIGUOUS, None, None, (
            f"{path} matches {len(matches)} registered devices ({names}) — REFUSING to guess.")
    name, dev = matches[0]
    return OK, name, dev, ""


def cmd_whois(reg: dict, args) -> int:
    rc, name, _dev, err = whois_path(reg, args.path)
    if rc != OK:
        print(f"devreg: {err}", file=sys.stderr)
        return rc
    print(name)
    return OK


def cmd_policy(reg: dict, args) -> int:
    """Print a device's policy block as shell-friendly key=value lines.

    Policy is data here. devreg knows which board a path is and what that board requires;
    it does not read firmware images, so the caller enforces the policy.
    """
    key = canonical_name(reg, args.name)
    if key is None:
        print(f"devreg: unknown device '{args.name}'", file=sys.stderr)
        return USAGE
    policy = reg["devices"][key].get("policy") or {}
    if args.key:
        val = policy.get(args.key)
        if val is None:
            return NOTFOUND
        print(val)
        return OK
    for k, v in sorted(policy.items()):
        if isinstance(v, dict):
            for kk, vv in sorted(v.items()):
                print(f"{k}.{kk}={vv}")
        else:
            print(f"{k}={v}")
    return OK


def cmd_busy(reg: dict, args) -> int:
    """Is a device's serial port held, and by whom?

    Complements `list`, which answers "is the board plugged in". A board can be plugged in
    and resolvable and still unusable because another process holds its port; the resulting
    error ("Could not exclusively lock port") reads like a missing board. This tells the two
    apart.

    Exit 0 free, 1 busy, so a script can branch on it.
    """
    names = [args.name] if args.name else sorted(reg["devices"])
    if args.name:
        key = canonical_name(reg, args.name)
        if key is None:
            print(f"devreg: unknown device '{args.name}'", file=sys.stderr)
            return USAGE
        names = [key]

    any_busy = False
    for name in names:
        rc, hit, _ = resolve_one(reg, name)
        if rc != OK:
            if args.name:
                print(f"{name:20s} {'absent':11s}")
            continue
        path = hit["identifier"]
        meta = devlock.probe(path)
        openers = devlock.open_by(path)
        if meta is None and not openers:
            print(f"{name:20s} {'free':11s} {'':60s} -> {path}")
            continue
        any_busy = True
        if meta is not None:
            print(f"{name:20s} {'BUSY':11s} {devlock.describe(meta)}")
        else:
            # Held by something that never took the lock (a hand-run serial CLI, a monitor):
            # reported as busy, since nothing else can open the port.
            print(f"{name:20s} {'BUSY':11s} (no devlock — holder found via /proc)")
        for o in openers:
            print(f"{'':20s} {'':11s}   pid {o['pid']} {o['comm']}: {o['cmdline'][:70]}")
        print(f"{'':20s} {'':11s}   -> {path}")
    return NOTFOUND if any_busy else OK


def cmd_deploy(reg: dict, args) -> int:
    """Copy a self-contained devreg bundle to a host, so it resolves natively.

    `where` and `--host` reach into a host from here, which covers lookups. A tool running on
    that host needs the registry locally; copying this one keeps a single implementation of
    the contract.

    A push, not a pull or a sync daemon: the copy is derived, the checkout here is the source,
    and the copy on the host is never edited.
    """
    import subprocess

    targets = args.hosts or [n for n, h in hosts(reg).items() if (h or {}).get("ssh")]
    here = Path(__file__).resolve().parent
    # The registry lands as registry.yaml whatever it is called here: the copy over there is
    # found beside the script by that name alone (_find_registry).
    files = [REGISTRY, here / "devreg.py", here / "devlock.py"]
    names = ["registry.yaml", "devreg.py", "devlock.py"]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        print(f"devreg: cannot deploy, missing: {', '.join(missing)}", file=sys.stderr)
        return USAGE

    rc_all = OK
    for name in targets:
        h = hosts(reg).get(name) or {}
        ssh = h.get("ssh")
        if not ssh:
            print(f"{name:10s} SKIP — no ssh in the registry", file=sys.stderr)
            continue
        dest = args.dir
        try:
            subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                            ssh, f"mkdir -p {dest}"], check=True,
                           capture_output=True, text=True, timeout=30)
            for f, n in zip(files, names):
                subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                str(f), f"{ssh}:{dest}/{n}"],
                               check=True, capture_output=True, text=True, timeout=120)
            # Run it there, so the report is "it works", not "it was copied".
            p = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", ssh,
                 f"timeout 30 python3 {dest}/devreg.py where 2>&1 | head -20"],
                capture_output=True, text=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            err = getattr(exc, "stderr", "") or str(exc)
            print(f"{name:10s} FAILED — {str(err).strip().splitlines()[-1:] or exc}",
                  file=sys.stderr)
            rc_all = NOTFOUND
            continue
        ok = p.returncode == 0 and "Traceback" not in p.stdout
        print(f"{name:10s} {'deployed' if ok else 'COPIED BUT DOES NOT RUN'} -> {dest}")
        for line in p.stdout.strip().splitlines():
            print(f"{'':10s}   {line}")
        if not ok:
            rc_all = NOTFOUND
    return rc_all


def lint(reg: dict) -> tuple[list[str], list[str]]:
    """(unknown, problems): the keys a registry carries that the contract (CONTRACT,
    docs/REGISTRY.md) does not know, each as `where: key (n devices)`, and the shapes that are
    wrong by the contract: a match block with no key that matches, a route with no state, a
    relative protected path, an alias two devices share, a bearer nobody speaks."""
    unknown: dict[str, list[str]] = {}
    problems: list[str] = []

    def odd(where: str, key: str, name: str):
        unknown.setdefault(f"{where}: {key}", []).append(name)

    for k in reg:
        if k not in CONTRACT["file"]:
            odd("file", k, "-")
    for hn, h in (reg.get("hosts") or {}).items():
        for k in (h or {}):
            if k not in CONTRACT["host"]:
                odd("hosts.*", k, hn)
        for p in (h or {}).get("protected") or []:
            if not str(p).startswith("/"):
                problems.append(f"hosts.{hn}.protected: {p!r} is not an absolute path")
    for p in reg.get("protected") or []:
        if not str(p).startswith("/"):
            problems.append(f"protected: {p!r} is not an absolute path")
    aliases: dict[str, list[str]] = {}
    for dn, d in (reg.get("devices") or {}).items():
        d = d or {}
        for k in d:
            if k not in CONTRACT["device"]:
                odd("devices.*", k, dn)
        for a in d.get("aliases") or []:
            aliases.setdefault(str(a), []).append(dn)
        blocks = [("match", d.get("match") or {})] + [("match_also[]", b or {}) for b in d.get("match_also") or []]
        for where, blk in blocks:
            for k in blk:
                if k not in CONTRACT["match"]:
                    odd(f"devices.*.{where}", k, dn)
            if where == "match" and blk and not (set(blk) & (CONTRACT["match"] - {"host"} - {"sibling_vid", "sibling_pid", "sibling_serial"})):
                problems.append(f"devices.{dn}.match: names nothing that matches a device")
        for bearer, r in (d.get("reach") or {}).items():
            r = r or {}
            if bearer not in BEARERS:
                odd("devices.*.reach", bearer, dn)
            for k in r:
                if k not in CONTRACT["reach"]:
                    odd("devices.*.reach.*", k, dn)
            if "state" not in r and bearer != "usb":
                problems.append(f"devices.{dn}.reach.{bearer}: no state (live, dormant or unproven)")
            if bearer in ("ble", "ssh", "wifi", "eth") and "addr" in r and "ip" in r and str(r["addr"]) != str(r["ip"]):
                problems.append(f"devices.{dn}.reach.{bearer}: addr and ip differ; `resolve --route` is ambiguous")
            for mn, m in (r.get("modes") or {}).items():
                for k in (m or {}):
                    if k not in CONTRACT["mode"]:
                        odd("devices.*.reach.*.modes.*", k, f"{dn}/{mn}")
    for a, names in aliases.items():
        if len(names) > 1 or a in (reg.get("devices") or {}):
            problems.append(f"alias {a!r} names more than one device: {', '.join(names)}")
    out = [f"{where} ({len(names)}: {', '.join(names[:4])}{', ...' if len(names) > 4 else ''})"
           for where, names in sorted(unknown.items())]
    return out, problems


def cmd_lint(reg: dict, args) -> int:
    """The keys this registry carries beyond the contract, and the shapes the contract refuses.
    Unknown keys are a site's own (its notes, its measurements): listed, not refused, unless
    --strict. A wrong shape is always rc 4."""
    unknown, problems = lint(reg)
    print(f"registry: {REGISTRY}")
    print(f"{len(reg.get('devices') or {})} devices, {len(reg.get('hosts') or {})} hosts")
    if unknown:
        print("keys the contract does not know (the site's own; docs/REGISTRY.md):")
        for line in unknown:
            print(f"  {line}")
    else:
        print("no keys beyond the contract")
    for line in problems:
        print(f"PROBLEM {line}")
    if problems:
        return USAGE
    return USAGE if (args.strict and unknown) else OK


def cmd_hosts(reg: dict, _args) -> int:
    """What hosts exist, which one this machine is, and whether each answers."""
    hs = hosts(reg)
    if not hs:
        print("(no hosts declared — devreg only looks at the local machine)")
        return OK
    lh = local_host(reg)
    for name in sorted(hs):
        h = hs[name] or {}
        marks = []
        if name == lh:
            marks.append("LOCAL")
        if h.get("bluetooth"):
            marks.append("bluetooth")
        if h.get("usbipd"):
            marks.append("usbipd")
        if h.get("protected"):
            marks.append(f"{len(h['protected'])} protected paths")
        pr = prober_for(reg, name)
        n = len(pr.usb())
        state = f"{n} usb devices" if not pr.error else f"UNREACHABLE ({pr.error})"
        print(f"{name:10s} {state:44s} {' '.join(marks)}")
        print(f"{'':10s} {h.get('description', '')}")
    return OK


def cmd_holders(_reg: dict, _args) -> int:
    """Every held lock on this host, registered or not.

    Deliberately not limited to the registry: a lock held on hardware nobody
    registered is exactly the thing you want to see when a port is mysteriously
    unavailable.
    """
    held = devlock.all_holders()
    if not held:
        print("(no serial locks held on this host)")
        return OK
    for meta in held:
        print(f"{meta.get('device') or meta['_key']}")
        print(f"    {devlock.describe(meta)}")
        if meta.get("purpose"):
            print(f"    purpose: {meta['purpose']}")
        print(f"    lockfile: {meta['_lockfile']}")
    return OK


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="devreg",
        description="Named hardware registry — resolve embedded targets by name, "
                    "never by raw device path.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="registered devices and whether they are present")
    p.add_argument("--host", help="look on this host instead of the local one")

    p = sub.add_parser("scan", help="present hardware not yet in the registry")
    p.add_argument("--host", help="look on this host instead of the local one")
    p.add_argument("--hubs", action="store_true",
                   help="also list USB hubs (hidden by default — a tiered hub is "
                        "otherwise most of the output on the bench host)")

    p = sub.add_parser("resolve", help="print the identifier for a named device")
    p.add_argument("name")
    p.add_argument("--host", help="look on this host instead of the local one")
    p.add_argument("--route", metavar="BEARER",
                   help="print the registry's address for this route (ble, ssh, wifi, eth) "
                        "instead of a USB path; nothing is probed")
    p.add_argument("--busid", action="store_true",
                   help="print where on the USB bus the device is (1-1.2.4), for a tool that "
                        "opens it by location (openocd's `adapter usb location`)")

    p = sub.add_parser("where", help="which host currently has a device (all hosts)")
    p.add_argument("name", nargs="?", help="omit to sweep every registered device")

    p = sub.add_parser("reach", help="every recorded route to a device, and what each can do")
    p.add_argument("name")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="also print the per-bearer/per-mode notes")

    sub.add_parser("hosts", help="hosts this registry knows about")

    p = sub.add_parser("deploy", help="push a self-contained devreg bundle to a host")
    p.add_argument("hosts", nargs="*", help="default: every host with an ssh entry")
    p.add_argument("--dir", default="~/.devreg",
                   help="destination directory on the remote (default ~/.devreg)")

    p = sub.add_parser("guard", help="assert a path is a named device (use before dd/flash)")
    p.add_argument("name")
    p.add_argument("path", help="a device path, or with --route an address")
    p.add_argument("--route", metavar="BEARER",
                   help="assert an address is the registry's for this route (ble, ssh, wifi)")

    p = sub.add_parser("whois", help="which registered device is at this path (inverse of resolve)")
    p.add_argument("path")

    p = sub.add_parser("policy", help="print a device's policy block as key=value lines")
    p.add_argument("name")
    p.add_argument("key", nargs="?", help="print just this key's value")

    p = sub.add_parser("busy", help="is a device's port held, and by whom (exit 1 = busy)")
    p.add_argument("name", nargs="?")

    sub.add_parser("holders", help="every serial lock held on this host")
    p = sub.add_parser("lint", help="keys beyond the contract (docs/REGISTRY.md) and wrong shapes")
    p.add_argument("--strict", action="store_true", help="exit 4 on keys beyond the contract too")

    args = ap.parse_args()
    reg = load_registry()
    return {
        "list": cmd_list,
        "scan": cmd_scan,
        "resolve": cmd_resolve,
        "where": cmd_where,
        "reach": cmd_reach,
        "hosts": cmd_hosts,
        "deploy": cmd_deploy,
        "guard": cmd_guard,
        "whois": cmd_whois,
        "policy": cmd_policy,
        "busy": cmd_busy,
        "holders": cmd_holders,
        "lint": cmd_lint,
    }[args.cmd](reg, args)


if __name__ == "__main__":
    sys.exit(main())
