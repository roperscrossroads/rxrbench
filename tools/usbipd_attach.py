#!/usr/bin/env python3
"""Attach the registry's workstation boards to WSL through usbipd-win.

    usbipd_attach.py [--host <key>] [--dry-run] [<name> ...]

On a WSL workstation a USB board reaches Linux only after `usbipd attach --wsl --busid <n>`,
and some boards drop off again whenever their app re-enumerates; each drop looks like "the
board is gone". This attaches, for each named device (default: every registry device whose
`reach.usb.host` is this workstation), every Windows bus id whose VID:PID is the device's, unless
the device's /dev/serial/by-id path is already there.

This workstation is --host, else the registry host devreg takes for the local machine
(`devreg.local_host`): the one whose key or `hostname` is this machine's hostname, else the one
marked `local: true`. A WSL machine's hostname is usually not its registry key, so a site gives
its workstation entry a `hostname:` or `local: true` (`site.example/registry.yaml`).

The VID:PID comes from the registry (`reach.usb.modes.app.usb`, else `match.usb_vid` /
`match.usb_pid`), never typed: bus ids are assigned by port and change. Two boards with one
VID:PID cannot be told apart from Windows, so every bus id with it is attached; that only makes
a device visible to Linux, and `devreg` still tells the boards apart by serial afterwards.
A device Windows has not shared yet needs `usbipd bind --busid <n>` once, as an administrator;
this reports that and does not try.
"""
import argparse
import os
import re
import subprocess
import sys
import time

import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402

DEVREG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "devreg", "devreg.py")

USBIPD = os.environ.get("USBIPD", "usbipd.exe")
ROW = re.compile(r"^\s*(\d+-\d+)\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(.*?)\s{2,}(\S.*?)\s*$")


def parse_list(text):
    """`usbipd list` -> [(busid, vid:pid, description, state)] for the Connected section."""
    rows, connected = [], False
    for line in text.splitlines():
        if line.strip().startswith("Connected"):
            connected = True
            continue
        if line.strip().startswith("Persisted"):
            break
        m = ROW.match(line) if connected else None
        if m:
            rows.append((m.group(1), m.group(2).lower(), m.group(3).strip(), m.group(4).strip()))
    return rows


def vid_pid(entry):
    usb = ((entry or {}).get("reach") or {}).get("usb") or {}
    vp = ((usb.get("modes") or {}).get("app") or {}).get("usb")
    if vp:
        return str(vp).lower()
    m = (entry or {}).get("match") or {}
    if m.get("usb_vid") and m.get("usb_pid"):
        return f"{m['usb_vid']}:{m['usb_pid']}".lower()
    return None


def by_id(entry):
    sid = ((entry or {}).get("match") or {}).get("serial_by_id")
    return f"/dev/serial/by-id/{sid}" if sid else None


def plan(devices, rows, names):
    """[(name, action, detail)]: what to do for each device, from the registry and the list."""
    out = []
    for name in names:
        e = devices.get(name)
        if e is None:
            out.append((name, "unknown", "not in the registry"))
            continue
        path = by_id(e)
        if path and os.path.exists(path):
            out.append((name, "present", path))
            continue
        vp = vid_pid(e)
        if not vp:
            out.append((name, "no-vidpid", "the registry gives no VID:PID for it"))
            continue
        hits = [r for r in rows if r[1] == vp]
        if not hits:
            out.append((name, "absent", f"{vp} is not plugged into the Windows host"))
        for busid, _vp, desc, state in hits:
            if state.lower().startswith("attached"):
                out.append((name, "attached", f"{busid} {desc}"))
            elif state.lower().startswith("not shared"):
                out.append((name, "not-shared", f"{busid} {desc}: run `usbipd bind --busid {busid}` "
                                                 "as an administrator once"))
            else:
                out.append((name, "attach", busid))
    return out


def workstation(reg):
    """The registry host key of this machine, by devreg's own rule (`devreg.local_host`)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("devreg", DEVREG)
    devreg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(devreg)
    return devreg.local_host(reg)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--host", help="the registry's host key for this workstation (default: "
                                   "devreg's local host)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    site.load_env()
    import yaml
    with open(site.registry_path()) as f:
        reg = yaml.safe_load(f) or {}
    devices = reg.get("devices") or {}
    host = a.host or (None if a.names else workstation(reg))
    if not a.names and not host:
        print("usbipd_attach: no registry host is this machine: pass --host, or give the "
              "workstation's registry entry `hostname: <this hostname>` or `local: true`",
              file=sys.stderr)
        return 2
    names = a.names or sorted(n for n, e in devices.items()
                              if (((e or {}).get("reach") or {}).get("usb") or {}).get("host") == host)
    try:
        listing = subprocess.run([USBIPD, "list"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"usbipd_attach: cannot run {USBIPD} ({e}): not a WSL workstation?", file=sys.stderr)
        return 2
    rc = 0
    for name, action, detail in plan(devices, parse_list(listing), names):
        if action == "attach" and not a.dry_run:
            r = subprocess.run([USBIPD, "attach", "--wsl", "--busid", detail],
                               capture_output=True, text=True, timeout=30)
            action = "attached now" if r.returncode == 0 else "FAILED"
            detail = detail if r.returncode == 0 else f"{detail}: {(r.stdout + r.stderr).strip()[-200:]}"
            rc |= r.returncode != 0
            time.sleep(1)
        print(f"{name:16} {action:13} {detail}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
