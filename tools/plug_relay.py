#!/usr/bin/env python3
"""Relay the bench hosts' plug watchers to a message command (and so to ntfy).

    plug_relay.py [--hosts h1,h2] [--dry-run]

Run every 30 s by the workstation's rxrbench-plug-relay timer. For each host it reads
~/rxrbench/state/usb-present.json (written by host/plugwatch.py) over ssh. A change in the set
of present devices is acted on only when the same set is seen on two runs in a row: a
a UF2 board re-enumerates during every flash, and that must not post. Then it runs the passive
survey for that host (`just survey <host>`: read-only, facts merged as usual) and posts what
appeared and disappeared, with the survey's line for each new device, to the message command as a
broadcast from `rxrbench-plug`.

The first run for a host records a baseline and posts nothing. State lives in
~/.local/state/rxrbench/plug-<host>.json. A bench host that cannot reach ntfy itself is
covered: only the workstation posts.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import hwsurvey, location, site  # noqa: E402
from ansible_collections.rxrbench.core.plugins.plugin_utils import families

site.load_env()
# the location index location.py reads (RXRBENCH_LOCATION_DIR moves both)
STATE_DIR = os.path.expanduser(os.environ.get("RXRBENCH_LOCATION_DIR", "~/.local/state/rxrbench"))
# The message post is optional: a command that takes `<sender> all <subject>` and the body on stdin.
SEND = os.path.expanduser(os.environ.get("RXRBENCH_BUS_SEND", ""))
SENDER = os.environ.get("RXRBENCH_BUS_SENDER", "rxrbench-plug")
STATE_VERSION = 2   # 2: boards keyed by chip id / MAC; 1: VID:PID:serial
# Each board that comes or goes gets its own notification here, titled with what it is, so
# an unlabelled board can be told apart by plugging it in.
BENCH_NTFY = os.environ.get("RXRBENCH_BENCH_NTFY", "")       # off unless the site sets it
FACTS_DIR = site.facts_dir()


def facts_by_serial():
    """The facts store, by USB serial: what each board was last seen to be. A list per serial:
    a serial is not always unique (a classic CP2102's factory default is "0001")."""
    import yaml  # pylint: disable=import-outside-toplevel
    out = {}
    for f in sorted(os.listdir(FACTS_DIR)) if os.path.isdir(FACTS_DIR) else []:
        if f.endswith(".yaml"):
            with open(os.path.join(FACTS_DIR, f)) as fh:
                rec = yaml.safe_load(fh) or {}
            serial = (rec.get("usb") or {}).get("serial")
            if serial:
                out.setdefault(serial, []).append(rec)
    return out


def lookup(known, d, host):
    """The facts record of USB device `d` seen on `host`: by serial, and by host as well when
    more than one board has that serial. None rather than a guess. A device the watcher
    identified in a bootloader that hides the chip id is looked up by that id."""
    recs = known.get(d.get("chip_id") or "") or known.get(d.get("serial") or "") or []
    if len(recs) > 1:
        recs = [r for r in recs if r.get("host") == host]
    return recs[0] if len(recs) == 1 else None


REGISTRY = site.registry_path()


def registry_descriptions():
    import yaml  # pylint: disable=import-outside-toplevel
    try:
        with open(REGISTRY) as fh:
            devices = (yaml.safe_load(fh) or {}).get("devices") or {}
    except OSError:
        return {}
    return {n: str((e or {}).get("description") or "") for n, e in devices.items()}


def sibling_name(d, devices):
    """A registry name for a device with no serial of its own, by the one match rule
    (hwsurvey.match_registry, devreg's rule): an entry whose match names
    `sibling_vid`/`sibling_pid`/`sibling_serial` is this device when its VID:PID matches and a
    device with those ids sits on the same parent hub (a board whose UART bridge sits beside
    its own second chip on an on-board hub). None when nothing, or more than one, matches."""
    import yaml  # pylint: disable=import-outside-toplevel
    try:
        with open(REGISTRY) as fh:
            entries = (yaml.safe_load(fh) or {}).get("devices") or {}
    except OSError:
        return None
    sibling_entries = {n: e for n, e in entries.items()
                       if any(k.startswith("sibling_") for k in ((e or {}).get("match") or {}))}
    found = hwsurvey.match_registry(d, sibling_entries, siblings=[o for o in devices if o is not d])
    return found[0] if len(found) == 1 else None


def short(text, n=90):
    text = text.split(" — ")[0].split(". ")[0].strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def identify(d, rec, alias=None):
    """(title name, body lines) for one device, from its facts when there are any; `alias`
    names a device the facts cannot (sibling_name)."""
    from ansible_collections.rxrbench.core.plugins.module_utils import hwsurvey  # noqa: E402
    rec = rec or {}
    names = rec.get("registered") or ([alias] if alias else [])
    c = rec.get("class") or hwsurvey.classify(d)
    pr = rec.get("probes") or {}
    mcu = next((x.split(":", 1)[1] for x in rec.get("tags", []) if x.startswith("mcu:")), None)
    what = c.get("probe") or c.get("bridge") or mcu or c.get("mcu") or c.get("kind") or "?"
    name = names[0] if len(names) == 1 else f"NEW {what}"
    lines = []
    desc = registry_descriptions().get(names[0], "") if len(names) == 1 else ""
    if desc:
        lines.append(short(desc))
    lines.append(f"{d.get('manufacturer') or '?'} {d.get('product') or '?'} ({what}, "
                 f"{d['usb_vid']}:{d['usb_pid']})")
    for fam in families.FAMILIES:
        # only a family whose probe ran on this board and found it (an empty record is found,
        # with nothing more known); asked of every board, a family would describe them all
        line = families.plugin(fam).survey_describe(pr[fam] or {}) if fam in pr else ""
        if line:
            lines.append(line)
    bl = pr.get("bootloader") or {}
    if bl.get("version"):
        lines.append(f"bootloader {bl['version']}")
    ids = dict(c.get("ids") or {})
    esp = pr.get("esptool") or {}
    if esp.get("mac") and "mac" not in ids:
        ids["mac"] = esp["mac"] + " (" + (esp.get("chip") or "chip") + ", read through the bridge)"
    lines.append(" ".join(f"{k} {v}" for k, v in ids.items()) or f"serial {d.get('serial')}")
    lines.append(f"USB port {d['busid']}")
    return name, lines


def ntfy(title, body, tags):
    if not BENCH_NTFY:
        return
    subprocess.run(["curl", "-s", "--max-time", "15", "-o", "/dev/null", "-H", f"Title: {title}",
                    "-H", f"Tags: {tags}", "--data-binary", "@-", BENCH_NTFY],
                   input=body, text=True, timeout=30, check=False)


def key(d):
    """A device's identity across plug-ins (location.device_key, shared with the layout)."""
    return location.device_key(d)


def label(d):
    return (f"{d.get('manufacturer') or '?'} {d.get('product') or '?'} "
            f"({d['usb_vid']}:{d['usb_pid']}{', serial ' + d['serial'] if d.get('serial') else ''}"
            f", port {d['busid']})")


BUSY_SEP = "RXRBENCH-BUSY-MARKS"


def read_marks(busy_dir, now=None):
    """The live busy marks in a host's busy directory. A mark whose time is up (its flash ended
    a while ago, or its run died) is deleted, so the directory holds only boards still busy."""
    now = time.time() if now is None else now
    marks = []
    for f in sorted(os.listdir(busy_dir)) if os.path.isdir(busy_dir) else []:
        if not f.endswith(".json"):
            continue
        path = os.path.join(busy_dir, f)
        try:
            with open(path) as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        if float(m.get("until") or 0) < now:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        marks.append(m)
    return marks


# The same on a bench host over ssh, judged by the host's own clock (it wrote the marks): the
# snapshot, the separator, one live mark per line; expired marks are deleted there.
REMOTE = ("import json, os, glob, time\n"
          "s = os.path.expanduser('~/rxrbench/state')\n"
          "print(open(s + '/usb-present.json').read())\n"
          f"print({BUSY_SEP!r})\n"
          "for f in sorted(glob.glob(s + '/busy/*.json')):\n"
          "    try:\n"
          "        m = json.load(open(f))\n"
          "    except Exception:\n"
          "        continue\n"
          "    if float(m.get('until') or 0) < time.time():\n"
          "        os.remove(f)\n"
          "        continue\n"
          "    print(json.dumps(m))\n")


def snapshot(host):
    """The host's USB snapshot, with the busy marks its flashers left (`busy`, a list)."""
    if host in site.local_hosts():      # this machine is the bench host: no ssh to itself
        state = os.path.expanduser(os.environ.get("RXRBENCH_HOST_STATE", "~/rxrbench/state"))
        with open(os.path.join(state, "usb-present.json")) as fh:
            snap = json.load(fh)
        snap["busy"] = read_marks(os.path.join(state, "busy"))
        return snap
    user, addr = site.bench_hosts().get(host, (None, host))
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                        f"{user}@{addr}" if user else addr,
                        "timeout 10 python3 -c " + shlex.quote(REMOTE)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"{host}: no snapshot ({(r.stderr or r.stdout).strip()[-200:]})")
    head, _, tail = r.stdout.partition(BUSY_SEP)
    snap = json.loads(head)
    marks = []
    for ln in tail.splitlines():
        try:
            marks.append(json.loads(ln)) if ln.strip() else None
        except ValueError:
            pass
    snap["busy"] = marks
    return snap


def registry_keys():
    """{board name and alias: its identity keys}, from the registry (location.identity)."""
    try:
        with open(site.registry_path()) as fh:
            reg = yaml.safe_load(fh) or {}
    except OSError:
        return {}
    out = {}
    for name, e in (reg.get("devices") or {}).items():
        keys = location.identity(e or {})
        for n in [name, *((e or {}).get("aliases") or [])]:
            out[str(n)] = keys
    return out


def hold_busy(devices, marks, before, names=None, now=None):
    """What a host has, with each busy board held as it was last reported: a board that is
    being flashed leaves the bus and comes back (often first as its bootloader, another USB
    device on the same port), and none of that is news. Devices on a busy port or with a busy
    board's identity are taken from `before` (the devices last seen) instead of the snapshot.
    A mark past its `until` is ignored (a run that died without removing it)."""
    now = time.time() if now is None else now
    names = registry_keys() if names is None else names
    ports, keys = set(), set()
    for m in marks or []:
        if float(m.get("until") or 0) < now:
            continue
        ports |= set(m.get("busids") or [])
        keys |= {str(k).upper() for k in (m.get("keys") or []) if k}
        keys |= names.get(str(m.get("board")), set())
    if not ports and not keys:
        return devices
    held = lambda d: d.get("busid") in ports or key(d).upper() in keys  # noqa: E731
    return [d for d in devices if not held(d)] + [d for d in (before or []) if held(d)]


def survey(host):
    """The passive survey for one host; returns its per-device lines by facts key."""
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-playbook"), "-i",
                        site.layout_path("hosts"), os.path.join(site.PLAYBOOKS, "survey.yml"),
                        "--limit", host],
                       cwd=REPO, capture_output=True, text=True, timeout=600)
    lines = []
    for ln in r.stdout.splitlines():
        ln = ln.strip().strip('",')
        if ln.startswith("new-") or ("[" in ln and "]" in ln and ";" in ln):
            lines.append(ln)
    return lines, r.returncode


def decide(state, devices):
    """(action, added, removed, new_state). action: baseline | wait | report | none."""
    current = {key(d): d for d in devices}
    reported = state.get("reported")
    if reported is None:
        return "baseline", [], [], {"reported": sorted(current), "pending": None}
    if sorted(current) == reported:
        return "none", [], [], {"reported": reported, "pending": None}
    if state.get("pending") != sorted(current):
        return "wait", [], [], {"reported": reported, "pending": sorted(current)}
    added = [current[k] for k in sorted(set(current) - set(reported))]
    removed = sorted(set(reported) - set(current))
    return "report", added, removed, {"reported": sorted(current), "pending": None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hosts", default=None, help="default: every bench host of the site")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)
    rc = 0
    for host in (a.hosts.split(",") if a.hosts else sorted(site.bench_hosts())):
        path = os.path.join(STATE_DIR, f"plug-{host}.json")
        state = json.load(open(path)) if os.path.exists(path) else {}
        if state.get("v") != STATE_VERSION:
            # Another key format (or none): what was reported cannot be compared with what is
            # seen now. Start again from a baseline instead of reporting every device swapped.
            state = {}
        try:
            snap = snapshot(host)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            print(e, file=sys.stderr)
            rc = 1
            continue
        snap["devices"] = hold_busy(snap["devices"], snap.get("busy"),
                                    list((state.get("devices") or {}).values()))
        action, added, removed, new_state = decide(state, snap["devices"])
        if action == "report":
            lines, src = survey(host) if added else ([], 0)
            known = facts_by_serial()
            gone_recs = [state.get("devices", {}).get(k) or {"busid": "?", "usb_vid": "?", "usb_pid": "?",
                                                            "serial": k}
                         for k in removed]
            body = [f"USB change on {host} (held across two readings):"]
            body += [f"+ {identify(d, lookup(known, d, host), sibling_name(d, snap['devices']))[0]}"
                     f": {label(d)}" for d in added]
            before = list((state.get("devices") or {}).values())
            body += [f"- {identify(d, lookup(known, d, host), sibling_name(d, before))[0]}"
                     f": {label(d)}" for d in gone_recs]
            if added:
                body += ["", "survey (passive, facts merged):" if src == 0 else
                         f"survey failed (rc {src}); run `just survey {host}`"]
                body += [f"  {ln}" for ln in lines if "[NEW]" in ln or
                         any(d.get("serial") and d["serial"] in ln for d in added)]
            subject = f"plug: {host} +{len(added)} -{len(removed)}"
            text = "\n".join(body)
            known = facts_by_serial()
            by_key = {key(d): d for d in snap["devices"]}
            gone = {k: state.get("devices", {}).get(k, {"busid": "?", "usb_vid": "?", "usb_pid": "?",
                                                       "serial": k})
                    for k in removed}
            notes = [("+", d) for d in added] + [("-", d) for d in gone.values()]
            for sign, d in notes:
                name, lines = identify(d, lookup(known, d, host))
                title = f"{'plugged in' if sign == '+' else 'unplugged'}: {name} on {host}"
                if a.dry_run:
                    print(title + "\n  " + "\n  ".join(lines))
                else:
                    ntfy(title, "\n".join(lines),
                         "electric_plug" if sign == "+" else "heavy_minus_sign")
            if a.dry_run:
                print(subject + "\n" + text)
            else:
                if SEND:
                    subprocess.run([SEND, SENDER, "all", subject], input=text, text=True,
                                   timeout=60, check=False)
        elif a.dry_run:
            print(f"{host}: {action}")
        # Remember every device seen until a report settles it, so a board that is unplugged
        # can still be described (its last snapshot record) when the removal is reported.
        current = {key(d): d for d in snap["devices"]}
        new_state["devices"] = current if action in ("report", "baseline") else \
            {**state.get("devices", {}), **current}
        new_state["v"] = STATE_VERSION
        if not a.dry_run:
            with open(path + ".tmp", "w") as fh:
                json.dump(new_state, fh)
            os.replace(path + ".tmp", path)
    return rc


if __name__ == "__main__":
    sys.exit(main())
