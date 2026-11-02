#!/usr/bin/env python3
"""rxrbench plug watcher: keep a snapshot of the USB devices present on this bench host.

Runs as a user service (rxrbench-plugwatch) under the host's `bench` venv (pyudev). On start,
and a few seconds after any USB device is added or removed, it writes

    ~/rxrbench/state/usb-present.json   {"host", "updated", "pid", "watching_since",
                                    "devices": [{busid, usb_vid, usb_pid, serial, product,
                                    manufacturer}, ...]}
    ~/rxrbench/state/plug-events.jsonl  one line when it starts (`"action": "start"`), then one
                                    per add/remove (capped at ~1 MB, then rotated once)

The log is the bench's presence history: a plan may stand a board's last full read in for
asking it only while one watcher has run without a break since that read and logged nothing
on the board's port (docs/ARCHITECTURE.md#checking-depth). `pid` and
`watching_since` say which watcher that is.

It reads sysfs, and never writes to a device or opens a serial port. One exception asks a
device something (docs/ARCHITECTURE.md#plug-watcher-and-busy-marks): a device whose VID:PID a board type
declares as a bootloader that hides the chip id (`uf2.hides_chip_id`, an RP2040's ROM, whose USB
serial is not its flash id) is asked, read-only, which of the registry's chip ids for that board
type selects it (`uf2.identify`: picotool's `info --bus --address --ser`). At most once per
enumeration of the device, with a timeout, and never while a flasher's busy mark is on its port.
The id that selects it goes into its record as `chip_id`; none, and it stays unidentified (its
bootloader's own serial is never taken for an identity); a failed ask says so (`identify_error`).

What a new device is, is the survey's job; the workstation's relay (tools/plug_relay.py) reads
the snapshot, waits for a change to hold, then runs the survey and posts a notification.
"""
import glob
import json
import os
import re
import socket
import subprocess
import threading
import time

try:
    import pyudev
except ImportError:      # the identity step is importable (and tested) without it
    pyudev = None

STATE = os.path.expanduser("~/rxrbench/state")
SNAPSHOT = os.path.join(STATE, "usb-present.json")
EVENTS = os.path.join(STATE, "plug-events.jsonl")
EVENTS_MAX = 1 << 20
SETTLE = 3.0          # a burst of events (a board re-enumerating) becomes one snapshot
CATALOG = os.path.expanduser("~/rxrbench/catalog/boards.yml")
REGISTRY = os.path.expanduser(os.environ.get("DEVREG_REGISTRY", "~/.devreg/registry.yaml"))
BUSY = os.path.join(STATE, "busy")
ASK_TIMEOUT = 10.0
RECHECK = 30.0        # a bootloader held back by a busy mark is looked at again this often
CHIP_ID = re.compile(r"[0-9A-Fa-f]{16}")


def rd(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def present():
    root, out = "/sys/bus/usb/devices", []
    for name in sorted(os.listdir(root)):
        dev = os.path.join(root, name)
        vid, pid = rd(os.path.join(dev, "idVendor")), rd(os.path.join(dev, "idProduct"))
        if vid is None or pid is None or rd(os.path.join(dev, "bDeviceClass")) == "09":
            continue     # interfaces, hubs
        out.append({"busid": name, "usb_vid": vid.lower(), "usb_pid": pid.lower(),
                    "serial": rd(os.path.join(dev, "serial")),
                    "product": rd(os.path.join(dev, "product")),
                    "manufacturer": rd(os.path.join(dev, "manufacturer")),
                    # which enumeration this is: an identity asked of it holds until it changes
                    "busnum": rd(os.path.join(dev, "busnum")),
                    "devnum": rd(os.path.join(dev, "devnum"))})
    return out


# ---- a bootloader that hides the chip id (docs/ARCHITECTURE.md#plug-watcher-and-busy-marks) -------------

def load_yaml(path):
    import yaml  # pylint: disable=import-outside-toplevel  (the bench venv has it)
    try:
        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}


def hiding_bootloaders(catalog, registry):
    """{"vid:pid": {"identify": tool, "ids": [chip id, ...]}}: each VID:PID a board type declares
    as a bootloader that hides the chip id, the query it names, and the registry's chip ids of
    the boards of those types (16 hex digits, a `usb_serial` of their `match` or `match_also`)."""
    out = {}
    for name, b in ((catalog or {}).get("boards") or {}).items():
        uf2 = (b or {}).get("uf2") or {}
        if not uf2.get("hides_chip_id") or not uf2.get("identify"):
            continue
        ids = []
        for e in ((registry or {}).get("devices") or {}).values():
            if ((e or {}).get("policy") or {}).get("board_tag") != name:
                continue
            for m in [(e or {}).get("match") or {}] + list((e or {}).get("match_also") or []):
                ser = str((m or {}).get("usb_serial") or "")
                if CHIP_ID.fullmatch(ser):
                    ids.append(ser.upper())
        for vp in uf2.get("bootloader_usb") or ():
            row = out.setdefault(str(vp).lower(), {"identify": uf2["identify"], "ids": []})
            row["ids"] = sorted(set(row["ids"]) | set(ids))
    return out


def busy_marks(busy_dir=BUSY, now=None):
    """(the USB ports, the chip ids) a flasher's live busy mark covers (flash/common.py `busy`).
    A picoboot session marks its chip id, not a port: it does not know the bootloader's yet."""
    now = time.time() if now is None else now
    ports, keys = set(), set()
    for f in glob.glob(os.path.join(busy_dir, "*.json")):
        try:
            with open(f) as fh:
                mark = json.load(fh)
        except (OSError, ValueError):
            continue
        if float(mark.get("until") or 0) >= now:
            ports |= {str(b) for b in mark.get("busids") or () if b}
            keys |= {str(k).upper() for k in mark.get("keys") or () if k}
    return ports, keys


def ask_picotool(d, chip_id, timeout=ASK_TIMEOUT, run=subprocess.run):
    """Does the bootloader at this bus and address select as chip `chip_id`? picotool reads the
    flash chip's unique id itself (`--ser`); a blank RP2040 prints none, so the selection is the
    proof, not an id in its output."""
    r = run(["picotool", "info", "--bus", str(d["busnum"]), "--address", str(d["devnum"]),
             "--ser", chip_id], capture_output=True, text=True, timeout=timeout, check=False)
    out = (r.stdout or "") + (r.stderr or "")
    # "Not this one" is picotool's words, whatever its exit status: "No accessible RP-series
    # devices ... with serial number X" (0), or with --bus/--address "No accessible RP-series
    # device ... at bus B, address A" (249). Anything else that is not an answer (no
    # permission, a crash) is an error, not a "no".
    if "No accessible RP-series device" in out:
        return False
    if r.returncode != 0:
        raise OSError(f"picotool info failed (rc {r.returncode}): {out.strip()[-160:]}")
    return True


#: the identity queries a board type may name (`uf2.identify`)
QUERIES = {"picotool": ask_picotool}

#: (busid, busnum, devnum) -> what was learned of that enumeration
_ASKED = {}


def identify(devices, hiding, busy, asked=None, queries=None):
    """Each device on a declared hiding bootloader gets `chip_id` (the registry's id that
    selects it), or `identify_error`, asked at most once per enumeration and never under a busy
    mark (`busy`: busy_marks' ports and chip ids; a mark naming any candidate id holds the ask
    back, since which bootloader its flasher holds is not known). The records are changed in
    place."""
    ports, keys = busy
    asked = _ASKED if asked is None else asked
    queries = QUERIES if queries is None else queries
    for d in devices:
        row = hiding.get(f"{d['usb_vid']}:{d['usb_pid']}")
        if not row:
            continue
        d["serial_is_id"] = False      # its serial is the bootloader's own, not the board's
        enum = (d["busid"], d.get("busnum"), d.get("devnum"))
        if enum in asked:
            d.update(asked[enum])
            continue
        if d["busid"] in ports or keys & set(row["ids"]):
            d["identify_deferred"] = True   # a flasher holds it: asked once it lets go
            continue
        ask = queries.get(row["identify"])
        got = {}
        if ask is None:
            got = {"identify_error": f"no query {row['identify']!r} in this watcher"}
        elif not d.get("busnum") or not d.get("devnum"):
            got = {"identify_error": "sysfs gives no bus number and address"}
        else:
            try:
                hits = [c for c in row["ids"] if ask(d, c)]
            except (OSError, subprocess.SubprocessError) as e:
                hits, got = [], {"identify_error": f"{row['identify']}: {e}", "retry": True}
            if len(hits) == 1:
                got = {"chip_id": hits[0], "identified_by": "bootloader"}
            elif len(hits) > 1:
                got = {"identify_error": f"more than one registered id selects it: {', '.join(hits)}"}
            elif not got:
                got = {"identify_error": "no registered chip id selects it"}
        if not got.get("retry"):
            asked[enum] = got           # a definite answer holds for the enumeration
        else:
            d["identify_deferred"] = True  # an error: asked again at the next look
        d.update({k: v for k, v in got.items() if k != "retry"})
    # forget enumerations that are gone
    here = {(d["busid"], d.get("busnum"), d.get("devnum")) for d in devices}
    for enum in [k for k in asked if k not in here]:
        del asked[enum]
    return devices


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


WATCHING_SINCE = now()


def write_snapshot():
    """The snapshot written; True when a device's identity was held back by a busy mark (the
    caller looks again: no USB event may follow the mark's end)."""
    devices = present()
    hiding = hiding_bootloaders(load_yaml(CATALOG), load_yaml(REGISTRY))
    if hiding:
        identify(devices, hiding, busy_marks())
    snap = {"host": socket.gethostname(), "updated": now(), "pid": os.getpid(),
            "watching_since": WATCHING_SINCE, "devices": devices}
    tmp = SNAPSHOT + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(snap, fh, indent=1)
    os.replace(tmp, SNAPSHOT)
    return any(d.get("identify_deferred") for d in devices)


def append(line):
    if os.path.exists(EVENTS) and os.path.getsize(EVENTS) > EVENTS_MAX:
        os.replace(EVENTS, EVENTS + ".1")
    with open(EVENTS, "a") as fh:
        fh.write(json.dumps(line) + "\n")


def log_event(action, dev):
    append({"t": now(), "action": action, "busid": dev.sys_name,
            "usb_vid": dev.properties.get("ID_VENDOR_ID"),
            "usb_pid": dev.properties.get("ID_MODEL_ID"),
            "serial": dev.properties.get("ID_SERIAL_SHORT")})


def main():
    os.makedirs(STATE, exist_ok=True)
    ctx = pyudev.Context()
    mon = pyudev.Monitor.from_netlink(ctx)
    mon.filter_by(subsystem="usb", device_type="usb_device")
    # listening before the start line is written: no event falls between the two
    mon.start()
    append({"t": WATCHING_SINCE, "action": "start", "pid": os.getpid()})
    timers = {"settle": None, "recheck": None}
    lock = threading.Lock()

    def schedule(which, delay, fn):
        if timers[which] is not None:
            timers[which].cancel()
        timers[which] = threading.Timer(delay, fn)
        timers[which].daemon = True
        timers[which].start()

    def settled():
        with lock:
            if write_snapshot():
                schedule("recheck", RECHECK, settled)

    settled()
    for dev in iter(mon.poll, None):
        if dev.action not in ("add", "remove"):
            continue
        log_event(dev.action, dev)
        schedule("settle", SETTLE, settled)


if __name__ == "__main__":
    main()
