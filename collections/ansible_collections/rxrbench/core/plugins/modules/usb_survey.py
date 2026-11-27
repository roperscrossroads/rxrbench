#!/usr/bin/python
"""Every USB device on a bench host: what it is, which registry entry it is, what it runs."""

DOCUMENTATION = r"""
module: usb_survey
short_description: Find and identify the USB devices on a bench host
description:
  - Enumerates the host's USB devices from sysfs, classifies each by what its descriptors
    say (VID:PID, serial, strings), and matches it to the device registry.
  - The passive pass changes nothing on any device. It asks a device what it runs only
    where asking is harmless; a firmware with a state line is asked for it (through its
    serial broker when it has one, else on the port under a devlock and an exclusive
    open); one without is asked by its plugin's own probe tool, where it has one; a family
    known only by a prose answer to a plain command (C(lineless)) is asked last, and only
    on a port nothing else has answered on; a board already in its UF2 bootloader has its
    INFO_UF2.TXT read from its drive.
  - C(deep) adds probes that reset a device. On an ESP32 board that is esptool and espefuse
    (chip revision, features, flash, MAC, eFuse summary, and the partition table, which says
    whether an app manager lives on the chip with images installed beside it), which put it
    in ROM download mode and reset it back into its app; a brokered board's console is
    released for it. A board whose power-on boots an app manager comes back in that manager,
    not in the app it was running, until the manager's own boot window passes.
  - Never writes to a device, never flashes, never enters a bootloader on its own.
options:
  registry:
    description: The device registry on this host.
    type: path
    default: ~/.devreg/registry.yaml
  devlock_dir:
    description: Where devlock.py lives on this host.
    type: path
    default: ~/.devreg
  boards:
    description: The board types (catalog/boards.yml C(boards)), for the MCU of a board a firmware names.
    type: dict
    default: {}
  deep:
    description: Run the probes that reset a device (esptool, espefuse). Not in check mode,
      where a reset would be a change. The passive survey runs then, and a warning says so.
    type: bool
    default: false
  only:
    description: Survey only these devices (registry names, or the C(key) of a new device).
      Default is every device on the host.
    type: list
    elements: str
    default: []
  esp_python:
    description: The venv whose esptool and espefuse the deep probe runs.
    type: path
    default: ~/.venv-esp
  host:
    description: This host's registry name, for entries whose match names a host.
    type: str
  timeout:
    description: Seconds allowed for each probe.
    type: float
    default: 40.0
  families:
    description: The device families as data (lookup C(rxrbench.core.bench_plugins) with
      C(what=families)). A family's C(survey) says which USB devices are asked for its state line
      and what is kept of it, or names the tool that probes a firmware with none.
    type: dict
    default: {}
  usb_kinds:
    description: The USB ids the loaded plugins bring for the boards their devices are (lookup
      C(rxrbench.core.bench_plugins) with C(what=usb_kinds)), beside the chip vendors', bridges'
      and probes' ids this collection knows.
    type: dict
    default: {}
  plugin_bin:
    description: Where the plugins' probe tools are on the bench host.
    type: path
    default: ~/rxrbench/bin
  picotool:
    description: picotool, which tells which registered RP2040 an unrecognised ROM bootloader is.
    type: str
    default: picotool
author:
  - Adam Roper (@roperscrossroads)
"""

RETURN = r"""
devices:
  description: One record per USB device that is not host infrastructure (hubs).
  returned: always
  type: list
  elements: dict
held:
  description: The USB ports of devices a flasher holds now (a live busy mark), neither asked
    nor recorded; surveyed the next time.
  returned: always
  type: list
  elements: str
"""

import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import (broker_client, esppart,
                                                                   familyspec, hwsurvey, location,
                                                                   stateline)
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common


def rd(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def usb_devices():
    root, out = "/sys/bus/usb/devices", []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        dev = os.path.join(root, name)
        vid, pid = rd(os.path.join(dev, "idVendor")), rd(os.path.join(dev, "idProduct"))
        if vid is None or pid is None:
            continue
        e = {"usb_vid": vid.lower(), "usb_pid": pid.lower(), "busid": name,
             "syspath": os.path.realpath(dev)}
        for opt in ("serial", "product", "manufacturer", "bDeviceClass", "bcdDevice", "speed",
                    "version"):
            v = rd(os.path.join(dev, opt))
            if v is not None:
                e[opt] = v
        # each interface as class/subclass/protocol (hex): what tells a keyboard from a board
        ifs = []
        for sub in sorted(os.listdir(dev)):
            if sub.startswith(name + ":"):
                got = [rd(os.path.join(dev, sub, f)) for f in
                       ("bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol")]
                if None not in got:
                    ifs.append("/".join(x.lower() for x in got))
        if ifs:
            e["interfaces"] = ifs
        out.append(e)
    return out


def rp2040_rom_owner(usb, devices, picotool="picotool", run=None):
    """Which registered board an RP2040's ROM bootloader is, when its USB face matched none.
    The ROM's USB serial is not the flash id the board is registered by, and a blank flash
    prints none; picotool selects a device only by the flash id it reads itself (`--ser`), so
    each registered 16-hex id is tried against this USB device alone (`--bus`, `--address`).
    Returns (name, flash id) or (None, None)."""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=20,
                                              check=False))
    bus, addr = rd(os.path.join(usb["syspath"], "busnum")), rd(os.path.join(usb["syspath"], "devnum"))
    if not bus or not addr:
        return None, None
    for name, entry in sorted((devices or {}).items()):
        fid = str(((entry or {}).get("match") or {}).get("usb_serial") or "")
        if not re.fullmatch(r"[0-9A-Fa-f]{16}", fid):
            continue
        try:
            r = run([picotool, "info", "-d", "--bus", bus, "--address", addr, "--ser", fid.upper()])
        except (OSError, subprocess.TimeoutExpired):
            return None, None
        out = (r.stdout or "") + (r.stderr or "")
        if "Device Information" in out and "No accessible" not in out:
            return name, fid.upper()
    return None, None


def watcher_owner(usb, devices, said):
    """(name, chip id) of the registered board the plug watcher identified this bootloader as,
    for the same enumeration (its busid and devnum), or (None, None)."""
    got = (said or {}).get((usb.get("busid"), rd(os.path.join(usb.get("syspath") or "", "devnum"))))
    fid = str((got or {}).get("chip_id") or "").upper()
    if not fid:
        return None, None
    for name, entry in sorted((devices or {}).items()):
        if str(((entry or {}).get("match") or {}).get("usb_serial") or "").upper() == fid:
            return name, fid
    return None, None


def owning_usb(devnode):
    base = os.path.basename(os.path.realpath(devnode))
    for p in (f"/sys/class/tty/{base}", f"/sys/block/{base}"):
        if os.path.exists(p):
            start = os.path.join(p, "device")
            node = os.path.realpath(start if os.path.exists(start) else p)
            while node and node != "/":
                if os.path.exists(os.path.join(node, "idVendor")):
                    return node
                node = os.path.dirname(node)
    return None


def by_id(dirpath, ttys="/sys/class/tty"):
    out, named = {}, set()
    for link in sorted(glob.glob(os.path.join(dirpath, "*"))):
        path = os.path.realpath(link)
        sp = owning_usb(path)
        if sp:
            out.setdefault(sp, []).append({"id": os.path.basename(link), "path": path})
            named.add(path)
    if dirpath.rstrip("/").endswith("serial/by-id"):
        # a USB tty with no by-id name of its own: two serial-less bridges of one make want the
        # same name and udev gives it to one, which left the other board with no port (devreg
        # lists such a tty too)
        for name in sorted(os.listdir(ttys)) if os.path.isdir(ttys) else []:
            path = "/dev/" + name
            if path in named or not os.path.exists(os.path.join(ttys, name, "device")):
                continue
            sp = owning_usb(path)
            if sp:
                out.setdefault(sp, []).append({"id": "", "path": path})
    return out


def holders(path):
    """Processes (pid, command) that have this device node open."""
    target, out = os.path.realpath(path), []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(os.path.join(fd_dir, fd)) == target:
                    cmd = (rd(f"/proc/{pid}/cmdline") or "").replace("\0", " ").strip()
                    out.append((int(pid), cmd[:120]))
                    break
            except OSError:
                continue
    return out


def mounts_of(block_ids):
    """Every mountpoint on these disks, partitions included (root lives on a partition)."""
    out = []
    for b in block_ids:
        rc, text = run(["lsblk", "-nro", "MOUNTPOINTS",
                        os.path.realpath(os.path.join("/dev/disk/by-id", b))], 10)
        if rc == 0:
            out += text.split()
    return out


def run(argv, timeout):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return 124, f"(killed after {timeout} s)"
    except OSError as e:
        return 127, str(e)


# ---- probes ------------------------------------------------------------------------------

def probe_console(name, tty, p, family, spec):
    """A family's state line, asked on the board's console: through the broker when one owns
    it, else on the port itself. Returns (what the line says, kept as the family's `survey`
    says; how it was asked), or (None, how) when the board printed none."""
    ident, survey = spec["identity"], spec["survey"]
    sock = broker_client.socket_path(name) if name else None
    if sock and os.path.exists(sock):
        with broker_client.Board(name, path=sock) as b:
            # a family that prints no state line (`lineless`) is asked its fallback alone
            line = None if ident.get("lineless") else stateline.read(b, ident, deadline=10)
            fallback = ident.get("fallback")
            if line is None and fallback:
                kw = {"dialect": ident["dialect"]} if ident.get("dialect") else {}
                out = b.exec(fallback["command"], deadline=10, **kw)
                if fallback["require"] in out:
                    got = familyspec.fields_of_fallback(fallback, out)
                    got.update(_also(ident, lambda cmd: b.exec(cmd, deadline=10, **kw)))
                    return _fallback_summary(ident, survey, got), "broker"
            return summarize(line, survey), "broker"
    held = holders(tty)
    if held:
        raise RuntimeError(f"port held by pid {held[0][0]} ({held[0][1]}): not opened")
    line = None if ident.get("lineless") else stateline.parse_line(
        console_exchange(tty, ident["command"], p), unknown=ident.get("null_tokens") or ())
    fallback = ident.get("fallback")
    if line is None and fallback:
        # Firmware from before the state line: its fallback command still names the image.
        # A lineless console has no shell prompt: its answer ends with the line itself.
        out = console_exchange(tty, fallback["command"], p, until=fallback["require"],
                               prompt=not ident.get("lineless"))
        if fallback["require"] in out:
            got = familyspec.fields_of_fallback(fallback, out)
            got.update(_also(ident, lambda cmd: console_exchange(tty, cmd, p, until="\n  -> ",
                                                                 prompt=False)))
            return _fallback_summary(ident, survey, got), "port"
    return summarize(line, survey), "port"


def _also(ident, ask):
    """What a family's identity asks after its fallback answered (`also`: [{command, field,
    pattern}], a role, say): {field: value} for each that answered as its pattern says."""
    out = {}
    for a in ident.get("also") or []:
        try:
            m = re.search(a["pattern"], ask(a["command"]), re.M)
        except Exception:  # pylint: disable=broad-except
            continue   # a question it does not answer leaves the field out
        if m:
            out[a["field"]] = m.group(1)
    return out


def _fallback_summary(ident, survey, got):
    """What a survey keeps of a fallback command's answer: an identity field's name in the probe
    is the survey key of the same state key."""
    names = {key: probe for probe, key in survey["keep"].items()}
    return {names[ident["fields"][f]]: v for f, v in got.items()
            if ident["fields"].get(f) in names} | {"no_state_line": True}


def _state_value(line, key):
    """A state key's value: `a.b` reads `b` inside the object `a`; None when absent."""
    value = line
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def summarize(line, survey):
    """What a survey keeps of a state line: `keep` {probe key: state key, `a.b` for a key inside
    an object}, and `flags` {probe key: state key} that are true when the line has the key at
    all."""
    if not line:
        return None
    keep = {k: _state_value(line, key) for k, key in survey["keep"].items()
            if _state_value(line, key) is not None}
    keep.update({k: True for k, key in (survey.get("flags") or {}).items() if key in line})
    return keep


def console_exchange(tty, command, p, until=None, prompt=True):
    """One shell command on a port no broker owns: devlock first, exclusive open. Read until
    a state line is there, or (`until`) that text and the prompt after it (`prompt` false: the
    end of the line that holds it)."""
    sys.path.insert(0, os.path.expanduser(p["devlock_dir"]))
    from devlock import DevLock  # pylint: disable=import-error,import-outside-toplevel
    import serial  # pylint: disable=import-outside-toplevel
    with DevLock(tty, tool="rxrbench-survey") as lk:
        if not lk.held:
            raise RuntimeError(f"port busy: {lk.holder_summary()}")
        with serial.Serial(tty, 115200, timeout=0.2, exclusive=True) as s:
            s.reset_input_buffer()
            s.write(b"\r\n" + command.encode() + b"\r\n")
            buf, end = b"", time.monotonic() + 4.0
            while time.monotonic() < end:
                buf += s.read(4096)
                if until is None:
                    if b"~S{" in buf and stateline.STATE_LINE.search(buf.decode(errors="replace")):
                        break
                elif until.encode() in buf and (
                        buf.count(b"$ ") >= 2 if prompt
                        else b"\n" in buf[buf.index(until.encode()):]):
                    break
            return buf.decode(errors="replace")


def probe_tool(tty, p, tool, probe=None):
    """A plugin's own probe of a board that has no state line: a script it deploys on the
    bench host, printing one JSON object. Run with the port; or, for a debug probe (no port),
    with `--probe <registry name> --wired <json>` (`probe`: (name, wired), the chips the
    registry wires to it). In a child process, so a library that hangs can be killed."""
    if probe is None:
        held = holders(tty)
        if held:
            raise RuntimeError(f"port held by pid {held[0][0]} ({held[0][1]}): not opened")
        args = [tty]
    else:
        args = ["--probe", probe[0], "--wired", json.dumps(probe[1])]
    script = os.path.join(os.path.expanduser(p["plugin_bin"]), tool)
    rc, out = run([sys.executable, script, *args], p["timeout"])
    lines = [ln for ln in out.splitlines() if ln.startswith("{")]
    if rc != 0 or not lines:
        raise RuntimeError(f"{tool} rc={rc}: {out.strip()[-200:]}")
    return json.loads(lines[-1])


def wired_to(probe, devices):
    """The chips the registry wires to debug probe `probe`: [{name, chip, uid, nrst}] of each
    entry whose `reach.swd.probe` names it."""
    out = []
    for name, e in sorted((devices or {}).items()):
        swd = ((e or {}).get("reach") or {}).get("swd") or {}
        if swd.get("probe") == probe:
            out.append({"name": name, "chip": swd.get("chip"), "uid": swd.get("uid"),
                        "nrst": bool(swd.get("nrst"))})
    return out


def mount_of(serial):
    """Where the USB drive of the device with this serial is mounted, or None."""
    for disk in glob.glob("/dev/disk/by-id/usb-*"):
        if serial not in disk or "-part" in disk:
            continue
        rc, out = run(["findmnt", "-rno", "TARGET,LABEL", "-S", os.path.realpath(disk)], 10)
        if rc == 0 and out.strip():
            target, _sp, label = out.strip().splitlines()[0].partition(" ")
            return target, label.strip()
    return None


def probe_drive(serial):
    """What a built-in debug probe's drive says the board is (its label, DETAILS.TXT,
    MBED.HTM), if it is mounted (hwsurvey.parse_probe_drive)."""
    got = mount_of(serial)
    if not got:
        return None
    mnt, label = got
    info = hwsurvey.parse_probe_drive(label or os.path.basename(mnt),
                                      rd(os.path.join(mnt, "DETAILS.TXT")) or "",
                                      rd(os.path.join(mnt, "MBED.HTM")) or "")
    if not info:
        return None
    info["mount"] = mnt
    return info


def read_unregistered_chip(usb, mcu, p):
    """The chip behind an unregistered built-in probe whose board type names it: its identity
    read through the probe at its USB location without halting or resetting it (swd_chip.py
    id --look), so the board can be registered with the unique id the survey read."""
    kind = "stlink" if usb.get("usb_vid") == "0483" else "cmsis-dap"
    tool = os.path.join(os.path.expanduser(p["plugin_bin"]), "swd_chip.py")
    rc, out = run([sys.executable, tool, "id", "--look", "--chip", mcu, "--probe", kind,
                   "--location", usb["busid"]], p["timeout"])
    lines = [ln for ln in out.splitlines() if ln.startswith("{")]
    got = json.loads(lines[-1]) if lines else {"error": out.strip()[-200:] or f"rc {rc}"}
    if rc != 0 and "error" not in got:
        got = {"error": f"rc {rc}"}
    return {"chip": mcu, **got}


def probe_info_uf2(serial):
    """INFO_UF2.TXT from a board already in its UF2 bootloader, if its drive is mounted."""
    got = mount_of(serial)
    mnt = got[0] if got else None
    if mnt and os.path.exists(os.path.join(mnt, "INFO_UF2.TXT")):
        info = hwsurvey.parse_info_uf2(rd(os.path.join(mnt, "INFO_UF2.TXT")) or "")
        info["mount"] = mnt
        return info
    return None


def enter_rom(venv, tty, mac, timeout):
    """Put a stock app on native USB (303a:0002) into ROM download mode and return the port it
    has there. esptool cannot use the app's port: the first connection reboots the chip into
    ROM, the app's port vanishes (that attempt fails, as expected) and the chip comes back as
    303a:1001 under another by-id name, derived from the MAC."""
    run([os.path.join(venv, "bin", "esptool"), "--port", tty, "--after", "no-reset", "chip-id"],
        timeout)
    want = hwsurvey.rom_port_path(mac)
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if os.path.exists(want):
            time.sleep(1)
            return want
        time.sleep(1)
    raise RuntimeError(f"{tty} did not enter ROM download mode ({want} never appeared)")


def probe_esp(name, tty, p, rom_mac=None):
    """esptool flash-id and espefuse summary: both reset the chip into ROM download mode and
    back into its app. A brokered console is released for the duration. `rom_mac` is set for a
    stock app on native USB, which has to be put into ROM download mode first."""
    venv = os.path.expanduser(p["esp_python"])
    sock = broker_client.socket_path(name) if name else None
    b = None
    if sock and os.path.exists(sock):
        b = broker_client.Board(name, path=sock).__enter__()
        b.release(ttl=3 * p["timeout"] + 30)
    info = None
    try:
        # espefuse first, esptool last: the last tool's hard reset is what puts the chip back
        # in its app (docs/HARDWARE.md, "ESP32-S3").
        after = "hard-reset"
        if rom_mac:
            tty, after = enter_rom(venv, tty, rom_mac, p["timeout"]), "no-reset"
        rc, out = run([os.path.join(venv, "bin", "espefuse"), "--port", tty, "--after",
                       after, "summary", "--format", "json"], p["timeout"])
        efuse, efuse_error, efuse_full, efuse_blocks = None, None, None, None
        if rc == 0 and "{" in out:
            try:
                ef = json.loads(out[out.index("{"):out.rindex("}") + 1])
                efuse = {k: v.get("value") for k, v in ef.items() if k in hwsurvey.EFUSE_KEEP}
                efuse_full, efuse_blocks = hwsurvey.efuse_all(ef)
            except ValueError:
                efuse_error = "unparseable espefuse output"
        else:
            efuse_error = out.strip()[-200:]
        # The partition table, so the facts say whether an app manager lives on the chip
        # with images installed beside it (what a flasher must never write over blind). Read
        # with no reset: the flash-id call that follows is the one that boots the board.
        partitions, partitions_error = None, None
        with tempfile.TemporaryDirectory() as d:
            table = os.path.join(d, "table.bin")
            rc, out = run([os.path.join(venv, "bin", "esptool"), "--port", tty, "--after",
                           "no-reset", "read-flash", f"{esppart.OFFSET:#x}",
                           f"{esppart.LENGTH:#x}", table], p["timeout"])
            if rc == 0 and os.path.exists(table):
                with open(table, "rb") as fh:
                    rows = esppart.parse(fh.read())
                partitions = {"manager": esppart.manager(rows),
                              "rows": [{"label": r["label"], "offset": r["offset"],
                                        "size": r["size"],
                                        "subtype": esppart.subtype_name(r["type"], r["subtype"])}
                                       for r in rows]}
                # What each region holds, as the write rule sees it (docs/ARCHITECTURE.md#partitions-and-the-write-rule):
                # the plan reports from this record, never from the chip.
                if rows:
                    partitions.update(survey_inventory(
                        rows, lambda off, n: _read_range(venv, tty, off, n, p["timeout"], d)))
            else:
                partitions_error = out.strip()[-200:]
        rc, out = run([os.path.join(venv, "bin", "esptool"), "--port", tty,
                       "--after", "hard-reset", "flash-id"], p["timeout"])
        if rc != 0:
            raise RuntimeError(f"esptool rc={rc}: {out.strip()[-300:]}")
        info = hwsurvey.parse_esptool(out)
        if efuse is not None:
            info["efuse"] = efuse
            info["efuse_all"] = efuse_full
            info["efuse_secret_blocks"] = efuse_blocks
        if efuse_error:
            info["efuse_error"] = efuse_error
        if partitions is not None:
            info["partitions"] = partitions
        if partitions_error:
            info["partitions_error"] = partitions_error
        return info
    finally:
        if b is not None:
            b.reacquire()
            try:
                back = False
                end = time.monotonic() + 30
                while time.monotonic() < end and not back:
                    try:
                        back = any(stateline.read(b, spec["identity"], deadline=5) is not None
                                   for spec in (p.get("families") or {}).values()
                                   if spec.get("identity") and not spec["identity"].get("dialect"))
                    except Exception:  # pylint: disable=broad-except
                        time.sleep(2)
                if not back:
                    # What was read stands: a board under an app manager boots on into an app
                    # whose console no family answers, and that is not a failed read. Whether
                    # it came back is its own finding.
                    note = ("no family's console answered after the deep probe: the board may "
                            "be in an app with no console a family knows (an app manager boots "
                            "on into one), or left in ROM download mode; reset it if it does "
                            "not run")
                    if info is None:
                        raise RuntimeError(note)
                    info["came_back"] = False
                    info["came_back_note"] = note
            finally:
                b.__exit__(None, None, None)


# ---- the survey ---------------------------------------------------------------------------

def survey(p):
    import yaml  # pylint: disable=import-outside-toplevel
    path = os.path.expanduser(p["registry"])
    if not os.path.exists(path):
        raise FileNotFoundError(f"no device registry on this host ({path}): `just hosts-setup hosts` "
                           "puts the site's registry and devreg on every bench host")
    with open(path) as fh:
        devices = (yaml.safe_load(fh) or {}).get("devices") or {}
    ttys = by_id("/dev/serial/by-id")
    disks = by_id("/dev/disk/by-id")
    records = []
    seen = list(usb_devices())
    # Every device's registry names first: a name two devices took is neither's (one entry
    # matching two bridges of the same make), as devreg refuses it. Filed under one name, the
    # second device's facts would overwrite the first's.
    names = {}
    for usb in seen:
        names[usb["syspath"]] = hwsurvey.match_registry(
            usb, devices, [x["id"] for x in ttys.get(usb["syspath"], [])], p["host"],
            siblings=seen, block_ids=[d["id"] for d in disks.get(usb["syspath"], [])])
    ambiguous = hwsurvey.ambiguous_names(
        [{"usb": u, "registered": names[u["syspath"]]} for u in seen
         if not hwsurvey.classify(u).get("infra")])
    # What a flasher holds now is not asked anything, and not filed: a board mid-flash shows
    # faces it does not keep (its bootloader), and a question can disturb the flash. It is
    # surveyed the next time, once the mark has lapsed.
    busy_ports, busy_keys = common.busy_marks()
    registered_ids = {str(((e or {}).get("match") or {}).get("usb_serial") or "").upper()
                      for e in devices.values()} - {""}
    held = []
    watcher = location.watcher_said()
    for usb in seen:
        c = hwsurvey.classify(usb)
        if c.get("infra"):
            continue
        if busy_ports or busy_keys:
            ids = {str(usb.get("serial") or "").upper()} | {
                str(v).upper() for v in (c.get("ids") or {}).values() if v}
            # an RP2040's ROM does not say which board it is: a mark naming any registered
            # board's id may be its flasher's
            rom = c.get("kind") == "rp2-bootrom" and c.get("mcu") == "rp2040" and \
                bool(busy_keys & registered_ids)
            if usb.get("busid") in busy_ports or ids & busy_keys or rom:
                held.append(usb.get("busid"))
                continue
        ports = ttys.get(usb["syspath"], [])
        blocks = [d["id"] for d in disks.get(usb["syspath"], [])]
        rec = {"usb": usb, "class": c,
               "registered": [n for n in names[usb["syspath"]] if n not in ambiguous],
               "serial_ports": ports,
               "block": blocks}
        # The host's own keyboard and mouse: never a device under test, unless registered
        if c["kind"] == "input" and not names[usb["syspath"]] and not ports and not blocks:
            continue
        shared = {n: ambiguous[n] for n in names[usb["syspath"]] if n in ambiguous}
        if shared:
            c["ambiguous"] = shared
        if c.get("kind") == "rp2-bootrom" and c.get("mcu") == "rp2040" and not rec["registered"]:
            # The plug watcher asks such a bootloader once per enumeration
            # (docs/ARCHITECTURE.md#plug-watcher-and-busy-marks): its answer for this very enumeration is
            # taken; asked here only when it has none (a host whose watcher predates it).
            owner, fid = watcher_owner(usb, devices, watcher)
            if not owner:
                owner, fid = rp2040_rom_owner(usb, devices, p.get("picotool") or "picotool")
            if owner:
                rec["registered"] = [owner]
                c.setdefault("ids", {})["chip_id"] = fid
                c["note"] = f"{owner} in its ROM bootloader (picotool found flash id {fid} here)"
                # a face of the board its registry entry does not have: devreg (no picotool)
                # sees the board in this mode once the entry lists it (match_also)
                c["unregistered_face"] = {"usb_vid": usb["usb_vid"], "usb_pid": usb["usb_pid"],
                                          "usb_serial": usb.get("serial", "")}
        if c["kind"] == "unknown" and rec["block"] and not ports:
            c["kind"] = "usb-storage"
            # The host's own disk (root or boot mounted from it) is infrastructure: reported
            # nowhere, never offered for registration as a device under test.
            if any(m in ("/", "/boot", "/boot/firmware") for m in mounts_of(rec["block"])):
                continue
        rec["key"] = hwsurvey.facts_key(rec, p["host"])
        if p["only"] and rec["key"] not in p["only"]:
            continue
        name = rec["registered"][0] if len(rec["registered"]) == 1 else None
        tty = rec["serial_ports"][0]["path"] if rec["serial_ports"] else None
        probes, errors = {}, {}
        kind, mode = c.get("kind"), c.get("mode")
        # What firmware answers: each family whose survey names this kind of USB device is
        # asked for its state line; then a family that probes with a tool of its own, when
        # the device says it runs that firmware (`modes`) or answered as no other (`unanswered`).
        # A family that prints no state line answers in prose that a family with one may
        # share (a firmware built on another's CLI): it is asked last, and only when no
        # family before it answered.
        for fam, spec in sorted((p.get("families") or {}).items(),
                                key=lambda kv: (bool((kv[1].get("identity") or {}).get("lineless")),
                                                kv[0])):
            survey = spec.get("survey") or {}
            if not (tty and spec.get("identity") and kind in (survey.get("kinds") or ())):
                continue
            lineless = spec["identity"].get("lineless")
            if lineless and probes:
                continue
            try:
                probe, via = probe_console(name, tty, p, fam, spec)
                if probe:
                    probes[fam] = {**probe, "via": via}
            except Exception as e:  # pylint: disable=broad-except
                # a console that does not echo or answer a lineless family's prose question
                # does not run it: not a failure worth reporting on every other board
                if not (lineless and ("no echo" in str(e) or "timeout" in str(e))):
                    errors[fam] = str(e)
        answered = bool(probes or errors)
        for fam, spec in sorted((p.get("families") or {}).items()):
            survey = spec.get("survey") or {}
            # a debug probe has no port: the tool reads the chips behind it, by the probe's
            # registry name (an unregistered probe: nothing says what is wired to it)
            if not tty and survey.get("tool") and kind in (survey.get("probes") or ()):
                if name:
                    try:
                        got = probe_tool(None, p, survey["tool"], (name, wired_to(name, devices)))
                        if got:
                            probes[fam] = got
                    except Exception as e:  # pylint: disable=broad-except
                        errors[fam] = str(e)
                continue
            if not (tty and survey.get("tool")):
                continue
            # a device that already said what it runs on its console is not asked again by a
            # tool (a second ask costs another family's probe its whole timeout)
            # (and none after a tool that answered: families are asked in name order)
            if (mode in (survey.get("modes") or ()) and not probes) or (
                    kind in (survey.get("unanswered") or ()) and not answered and not probes):
                try:
                    got = probe_tool(tty, p, survey["tool"])
                    if got:   # an empty answer: not this family's firmware
                        probes[fam] = got
                except Exception as e:  # pylint: disable=broad-except
                    errors[fam] = str(e)
        if mode == "bootloader" and usb.get("serial"):
            bl = probe_info_uf2(usb["serial"])
            if bl:
                probes["bootloader"] = bl
        if kind == "swd-probe" and usb.get("serial"):
            try:
                drive = probe_drive(usb["serial"])
                if drive:
                    probes["probe_drive"] = drive
                    btype = hwsurvey.board_of_drive(drive.get("label"), p.get("boards"))
                    mcu = ((p.get("boards") or {}).get(btype) or {}).get("mcu")
                    # A registered probe's chips are its family's tool's to read (the registry
                    # says what is wired to it); an unregistered built-in one's, by its drive.
                    if btype and mcu and not name and usb.get("busid"):
                        probes["swd"] = {"board_type": btype, **read_unregistered_chip(usb, mcu, p)}
            except Exception as e:  # pylint: disable=broad-except
                errors["probe_drive"] = str(e)
        try:
            # Behind a USB-UART bridge the board is invisible to USB, and identical bridges share
            # a serial (two CP2102s reporting `0001`): esptool through the bridge reads the
            # ESP32's own MAC, an identity no port gives. A non-ESP target just fails the probe.
            if p["deep"] and tty and kind in ("esp32-usb-jtag", "esp32-usb-cdc", "uart-bridge"):
                probes["esptool"] = probe_esp(
                    name, tty, p,
                    rom_mac=(c.get("ids") or {}).get("mac") if kind == "esp32-usb-cdc" else None)
        except Exception as e:  # pylint: disable=broad-except
            errors["esptool"] = str(e)
        rec["probes"], rec["probe_errors"] = probes, errors
        rec["tags"] = hwsurvey.tags(rec, p.get("boards") or {})
        records.append(rec)
    p["held"] = held      # the ports a flasher held: surveyed next time
    return records


def _read_range(venv, tty, off, n, timeout, d):
    """`n` bytes at `off`, read with no reset (the survey's flash-id call boots the board)."""
    path = os.path.join(d, f"r{off:x}.bin")
    rc, _out = run([os.path.join(venv, "bin", "esptool"), "--port", tty, "--after", "no-reset",
                    "read-flash", f"{off:#x}", f"{n:#x}", path], timeout)
    if rc != 0 or not os.path.exists(path):
        return b""
    with open(path, "rb") as fh:
        return fh.read()


def survey_inventory(rows, read):
    """{boot, inventory}: the app the chip boots and each region's owner, for the facts."""
    ota = next((r for r in rows if r["type"] == esppart.DATA and r["subtype"] == esppart.SUB_OTADATA),
               None)
    otadata = read(ota["offset"], min(ota["size"], 0x2000)) if ota else None
    boot = esppart.boot_app(rows, otadata)
    inv = esppart.inventory(rows, otadata, read)
    return {"boot": boot["label"] if boot else None,
            "inventory": [{"label": r["label"], "offset": r["start"], "size": r["size"],
                           "kind": r["kind"], "owner": r["owner"],
                           "recogniser": (r.get("evidence") or {}).get("recogniser")}
                          for r in inv if r["kind"] in ("app", "data")]}


def main():
    module = AnsibleModule(
        argument_spec={
            "registry": {"type": "path", "default": "~/.devreg/registry.yaml"},
            "devlock_dir": {"type": "path", "default": "~/.devreg"},
            "deep": {"type": "bool", "default": False},
            "boards": {"type": "dict", "default": {}},
            "families": dict(familyspec.ARGUMENT),
            "usb_kinds": {"type": "dict", "default": {}},
            "plugin_bin": {"type": "path", "default": "~/rxrbench/bin"},
            "picotool": {"type": "str", "default": "picotool"},
            "only": {"type": "list", "elements": "str", "default": []},
            "esp_python": {"type": "path", "default": "~/.venv-esp"},
            "timeout": {"type": "float", "default": 40.0},
            "host": {"type": "str"},
        },
        supports_check_mode=True,
    )
    p = dict(module.params)
    if p.get("usb_kinds"):       # a module is not shipped the plugins: their ids come as data
        hwsurvey.register_kinds(p["usb_kinds"])
    if module.check_mode and p["deep"]:
        # The deep probe resets a board (esptool, espefuse). Check mode changes nothing on a
        # device, a reset included: the passive survey runs, and says what it left out.
        p["deep"] = False
        module.warn("check mode: the deep probe resets ESP32 boards and was not run")
    try:
        devices = survey(p)
        module.exit_json(changed=False, devices=devices, held=p.get("held") or [])
    except (OSError, ValueError) as e:
        module.fail_json(msg=str(e))


if __name__ == "__main__":
    main()
