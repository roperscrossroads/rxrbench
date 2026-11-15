"""Which bench host a board is plugged into now, from the plug watcher, without searching.

The workstation's plug relay (tools/plug_relay.py) reads every bench host's USB snapshot
every 30 s and keeps what it saw in ~/.local/state/rxrbench/plug-<host>.json. That is a
location index for free: "where is board1?" is a lookup in local files, no ssh, no sweep.

A board is found by its identity, never by a port: the chip id in its registry match
(`usb_serial`, an nRF52's FICR id in every USB mode) or the MAC in its `serial_by_id` (the
ESP32's USB-Serial-JTAG). A layout's or the registry's host is a hint, used when the board
is not seen (unplugged, the relay not running, a device with no identity of its own).
"""
import json
import os
import re
import time

MAC = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
FRESH = 600          # a relay state older than this is not trusted (the relay may be stopped)


def device_key(d):
    """A USB device's identity across plug-ins, as the relay files it (d: busid, usb_vid,
    usb_pid, serial). A serial that is a chip id or a MAC is the board itself, whatever mode it
    enumerates in (its VID:PID changes between its factory app, another app and its
    bootloader, and a first flash is not an unplug and a plug). Any other serial (or none) is
    only good together with VID:PID, or the port. A device the watcher identified on a
    bootloader that hides the chip id has `chip_id`: that is its key; its own serial is not an
    identity (`serial_is_id: false`), and never read as a MAC (docs/ARCHITECTURE.md#plug-watcher-and-busy-marks)."""
    if d.get("chip_id"):
        return str(d["chip_id"]).upper()
    s = d.get("serial") or ""
    if d.get("serial_is_id") is False:
        return f"{d['usb_vid']}:{d['usb_pid']}:{s or 'port-' + d['busid']}"
    if re.fullmatch(r"[0-9A-F]{16}", s) or re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", s):
        return s.upper()
    if re.fullmatch(r"[0-9A-Fa-f]{12}", s):
        # A stock ESP32 app on native USB gives the MAC without colons; its ROM and
        # USB-Serial-JTAG faces give it with them. One board, one key.
        return ":".join(s[i:i + 2] for i in range(0, 12, 2)).upper()
    return f"{d['usb_vid']}:{d['usb_pid']}:{s or 'port-' + d['busid']}"


def usb_now(sysfs=None):
    """The USB devices on this machine now, read from sysfs (no hubs, no interfaces)."""
    sysfs = sysfs or os.environ.get("RXRBENCH_SYSFS_USB", "/sys/bus/usb/devices")
    def rd(path):
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            return None
    out = []
    if not os.path.isdir(sysfs):
        return out
    for name in sorted(os.listdir(sysfs)):
        dev = os.path.join(sysfs, name)
        vid, pid = rd(os.path.join(dev, "idVendor")), rd(os.path.join(dev, "idProduct"))
        if vid is None or pid is None or rd(os.path.join(dev, "bDeviceClass")) == "09":
            continue
        out.append({"busid": name, "usb_vid": vid.lower(), "usb_pid": pid.lower(),
                    "serial": rd(os.path.join(dev, "serial")),
                    "devnum": rd(os.path.join(dev, "devnum"))})
    return out


def watcher_said(snapshot=None):
    """{(busid, devnum): what the plug watcher learned of that enumeration}: `chip_id`,
    `serial_is_id` (host/plugwatch.py, from its snapshot on this machine)."""
    snapshot = snapshot or os.path.join(os.path.expanduser(os.environ.get(
        "RXRBENCH_HOST_STATE", "~/rxrbench/state")), "usb-present.json")
    try:
        with open(snapshot) as fh:
            snap = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for d in snap.get("devices") or ():
        said = {k: d[k] for k in ("chip_id", "serial_is_id") if k in d}
        if said and d.get("busid"):
            out[(d["busid"], str(d.get("devnum")))] = said
    return out


def seen_here(hosts, sysfs=None, snapshot=None):
    """{identity key: host} for a bench host that is this machine (`hosts`: its names, the
    first used), read live: the relay's files lag a reflash by up to a minute, sysfs does not."""
    hosts = sorted(hosts or ())
    if not hosts:
        return {}
    said = watcher_said(snapshot)
    out = {}
    for d in usb_now(sysfs):
        # what the watcher learned of this very enumeration (a bootloader's chip id)
        d.update(said.get((d["busid"], str(d.get("devnum"))), {}))
        out[device_key(d).upper()] = hosts[0]
    return out


def identity(entry):
    """The keys the relay files this registry entry's device under (upper case), or set():
    from its `match` and every `match_also` face. A MAC without colons (a stock ESP32 app's
    bare serial) is the same board as the MAC with them (its ROM / USB-Serial-JTAG face)."""
    out = set()
    for m in [(entry or {}).get("match") or {}] + list((entry or {}).get("match_also") or []):
        serial = str(m.get("usb_serial") or "")
        if re.fullmatch(r"[0-9A-Fa-f]{16}", serial):
            out.add(serial.upper())
        elif re.fullmatch(r"[0-9A-Fa-f]{12}", serial):
            out.add(":".join(serial[i:i + 2] for i in range(0, 12, 2)).upper())
            # twelve hex digits are not always a MAC: an RP2040's ROM bootloader has such a
            # serial, and the relay files it VID:PID:serial
            if m.get("usb_vid") and m.get("usb_pid"):
                out.add(f"{m['usb_vid']}:{m['usb_pid']}:{serial}".upper())
        by_id = str(m.get("serial_by_id") or "")
        found = MAC.search(by_id)
        if found:
            out.add(found.group(0).upper())
        else:
            bare = re.search(r"_([0-9A-Fa-f]{12})-if", by_id)
            seg = re.search(r"_([^_/]+)-if\d\d", by_id)
            if bare:
                out.add(":".join(bare.group(1)[i:i + 2] for i in range(0, 12, 2)).upper())
            elif seg and _custom_serial(seg.group(1)):
                # A USB bridge given a serial of its own (so that two of a kind can be told
                # apart): the relay keys it VID:PID:serial, and the serial alone names it.
                out.add("*:" + seg.group(1).upper())
    return out


def _custom_serial(s):
    """A bridge serial that names one bridge: anything but a bare number, which is the factory
    default (`0001`) every bridge of a model ships with."""
    return bool(s) and not s.isdigit()


def factory_serial(entry):
    """The factory serial a registry entry's USB bridge still carries (a bare number in its
    `serial_by_id`), or None: such a bridge cannot be told from another of its model, so the
    watcher never sees its board. The remedy is a serial of the bridge's own."""
    for m in [(entry or {}).get("match") or {}] + list((entry or {}).get("match_also") or []):
        seg = re.search(r"_([^_/]+)-if\d\d", str(m.get("serial_by_id") or ""))
        if seg and seg.group(1).isdigit():
            return seg.group(1)
    return None


def seen(state_dir=None, now=None):
    """{identity key: host} from every fresh relay state file."""
    state_dir = os.path.expanduser(state_dir or os.environ.get(
        "RXRBENCH_LOCATION_DIR", "~/.local/state/rxrbench"))
    now = now if now is not None else time.time()
    out = {}
    if not os.path.isdir(state_dir):
        return out
    for f in sorted(os.listdir(state_dir)):
        m = re.fullmatch(r"plug-(.+)\.json", f)
        if not m:
            continue
        path = os.path.join(state_dir, f)
        if now - os.path.getmtime(path) > FRESH:
            continue
        try:
            with open(path) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            continue
        # What the relay last reported, and what its latest run saw but has not yet seen twice
        # (`pending`): a board that was just reflashed or replugged is back before the relay
        # settles, and a plan right after must not take it for unplugged. Erring toward present
        # costs only a read that says the board is not there.
        for key in list(st.get("reported") or []) + list(st.get("pending") or []):
            out[str(key).upper()] = m.group(1)
    return out


def seen_models(state_dir=None, now=None):
    """{"VID:PID": host} for every device in a fresh relay state file: what is there of a
    model, for a device the registry knows by its VID:PID alone (a probe with no serial). Two
    hosts with one each: the first in name order (presence is the question, not which)."""
    state_dir = os.path.expanduser(state_dir or os.environ.get(
        "RXRBENCH_LOCATION_DIR", "~/.local/state/rxrbench"))
    now = now if now is not None else time.time()
    out = {}
    if not os.path.isdir(state_dir):
        return out
    for f in sorted(os.listdir(state_dir)):
        m = re.fullmatch(r"plug-(.+)\.json", f)
        if not m:
            continue
        path = os.path.join(state_dir, f)
        if now - os.path.getmtime(path) > FRESH:
            continue
        try:
            with open(path) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            continue
        for d in (st.get("devices") or {}).values():
            if d.get("usb_vid") and d.get("usb_pid"):
                out.setdefault(f"{d['usb_vid']}:{d['usb_pid']}".lower(), m.group(1))
    return out


def locate(entry, hint=None, index=None):
    """(host, how): where the board is seen, else the hint. `how` is 'seen' or 'hint'."""
    index = seen() if index is None else index
    ids = identity(entry)
    hosts = {index[k] for k in ids if k in index}
    for k in ids:
        if k.startswith("*:"):   # a bridge's own serial, under whatever VID:PID it reports
            hosts |= {h for ik, h in index.items() if ik.endswith(k[1:]) and ik.count(":") == 2}
    if len(hosts) == 1:
        return hosts.pop(), "seen"
    return hint, "hint"
