#!/usr/bin/env python3
"""Enroll a new device: from what the survey learned to a registry entry (and a layout entry).

    enroll.py <name> <facts key | chip id | MAC> [--layout L --key V] [--dry-run]

1. Finds the device's facts (`<facts>/new-…`, written by `just survey`).
2. Refuses a name (or alias) the registry already has, and a device it already knows.
3. Writes a registry entry built from the facts: what the device is (description, hardware
   from its tags), how it is matched (an nRF52840 under a UF2 bootloader by its chip id, which that
   bootloader and its apps all report as the USB serial; an ESP32 by the /dev/serial/by-id name that carries its MAC), where it is plugged in
   (`reach.usb.host`, a hint: the plug watcher knows where it is now), and its identity as
   delivered. The registry keeps only what does not change: firmware, bootloader and node id
   live in the generated facts.
4. Checks the registry still parses and that nothing but the new entry changed, then files the
   facts under the name and pushes the registry to every bench host (`devreg deploy`).
A board with a debug probe built in (a Nucleo's ST-Link) is two entries: the probe (`kind: usb`,
matched by its own USB serial) and the board (`kind: swd`, its chip by the unique id the survey
read through the probe, and its console on the probe's serial port when it has one). Give either
the probe's serial or the chip's unique id; the probe's name is `--probe-name`, or `stlink-<name>`
for an ST-Link. Nothing is guessed: without a chip read through the probe, a probe serial of its
own and exactly one board type, nothing is written and the refusal says what is missing.

5. With --layout and --key (the value of what its family tells its images apart by): adds
   the board to that layout with the first-flash opt-in, so
   `just boot <layout>` and `just apply <layout>` take it from its factory state.

Everything it writes is plain text in git, for review.
"""
import argparse
import os
import re
import subprocess
import sys

import yaml

import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402
from ansible_collections.rxrbench.core.plugins.plugin_utils import families

site.load_env()
REGISTRY = site.registry_path()
FACTS = site.facts_dir()


DEVREG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "devreg", "devreg.py")


def push_registry(reg):
    """The registry to every bench host: over ssh by this checkout's devreg (`devreg deploy`),
    and in place on this machine when it is a bench host itself (`just hosts-setup` left a
    copy in ~/.devreg: a workstation that is its own bench host reads that one)."""
    env = dict(os.environ, DEVREG_REGISTRY=REGISTRY)
    for h in sorted((reg.get("hosts") or {}).keys()):
        if (reg["hosts"][h] or {}).get("ssh"):
            r = subprocess.run([sys.executable, DEVREG, "deploy", h], capture_output=True,
                               text=True, check=False, env=env)
            print(f"registry -> {h}: {'ok' if r.returncode == 0 else 'FAILED ' + r.stderr[-200:]}")
    here = os.path.expanduser("~/.devreg/registry.yaml")
    if os.path.exists(here) and os.path.realpath(here) != os.path.realpath(REGISTRY):
        with open(REGISTRY) as src, open(here + ".tmp", "w") as dst:
            dst.write(src.read())
        os.replace(here + ".tmp", here)
        print(f"registry -> this machine: ok ({here})")


def find_facts(ident):
    want = ident.upper().replace(":", "")
    hits = []
    for f in sorted(os.listdir(FACTS)):
        if not f.endswith(".yaml"):
            continue
        with open(os.path.join(FACTS, f)) as fh:
            rec = yaml.safe_load(fh) or {}
        ids = (rec.get("class") or {}).get("ids") or {}
        read = ((rec.get("probes") or {}).get("esptool") or {}).get("mac") or ""
        # a chip behind a built-in probe, by the unique id the survey read through it
        swd_uid = str(((rec.get("probes") or {}).get("swd") or {}).get("uid") or "").upper()
        keys = {rec.get("key", ""), (ids.get("chip_id") or "").upper(),
                (ids.get("mac") or "").upper().replace(":", ""), read.upper().replace(":", ""),
                str((rec.get("usb") or {}).get("serial") or "").upper(), swd_uid} - {""}
        if ident in keys or want in keys:
            hits.append((f, rec))
    if len(hits) != 1:
        sys.exit(f"{ident}: {len(hits)} facts files match (want exactly one): "
                 + ", ".join(h[0] for h in hits) + " -- run `just survey <host>` first")
    return hits[0]


def own_serial(serial):
    """Does a USB serial name one unit? A chip id or MAC does; a short or all-digit serial is a
    factory default every device of the model ships with (a bridge's `0001`)."""
    s = str(serial or "")
    return len(s) >= 8 and not s.isdigit() and re.fullmatch(r"[0-9A-Za-z_-]+", s) is not None


def board_types(rec, boards):
    """The board types (catalog/boards.yml) the survey's facts fit: by the Zephyr board name a
    firmware reported (tag `board:`), for a board in its UF2 bootloader by the bootloader's
    USB id, for a board behind its built-in debug probe by the probe's drive label (a type's
    `probe_drive`), and by the kind of USB device the survey named when that kind is a type's name (a
    stock app names no Zephyr board, but the survey knows the kind of board). A type is the
    registry's `policy.board_tag`: what an image may go on."""
    tag = {t.split(":", 1)[0]: t.split(":", 1)[1] for t in rec.get("tags") or [] if ":" in t}
    kind = (rec.get("class") or {}).get("kind")
    usb = rec.get("usb") or {}
    vidpid = f"{usb.get('usb_vid', '')}:{usb.get('usb_pid', '')}".lower()
    out = []
    for name, b in sorted((boards or {}).items()):
        b = b or {}
        if tag.get("board") and tag["board"] in (b.get("zephyr_boards") or []):
            out.append(name)
        elif vidpid in [str(u).lower() for u in ((b.get("uf2") or {}).get("bootloader_usb") or [])]:
            out.append(name)
        elif tag.get("probe-drive") and tag["probe-drive"] == b.get("probe_drive"):
            out.append(name)
        elif kind and kind == name and not tag.get("board"):
            out.append(name)
    return out


def entry_for(name, rec, boards=None):
    c, probes, tags = rec.get("class") or {}, rec.get("probes") or {}, rec.get("tags") or []
    usb, ids = rec.get("usb") or {}, c.get("ids") or {}
    # what the families' probes add: a hardware model, what the board arrived running
    extra = {}
    for fam in families.FAMILIES:
        extra.update(families.plugin(fam).enroll_note(probes.get(fam) or {}))
    tag = {t.split(":", 1)[0]: t.split(":", 1)[1] for t in tags if ":" in t}
    if ids.get("chip_id"):
        match = {"usb_serial": ids["chip_id"]}
        note = "the chip's own id, the same in its app and its bootloader"
    elif ids.get("mac") and [x for x in rec.get("serial_ports") or () if not x.startswith("/dev/")]:
        match = {"serial_by_id": [x for x in rec["serial_ports"] if not x.startswith("/dev/")][0]}
        note = "carries the chip's MAC"
    elif c.get("kind") == "uart-bridge" and (probes.get("esptool") or {}).get("mac"):
        # A bridge with no serial of its own (a CH340 has none): nothing on USB names the
        # board, so it is matched by make, and the chip's MAC, which the deep survey read
        # through the bridge, is what a flash checks before it writes (policy.chip_mac).
        match = {"usb_vid": usb["usb_vid"], "usb_pid": usb["usb_pid"]}
        note = "a bridge with no serial: matched by VID:PID, the chip told by policy.chip_mac"
    elif own_serial(usb.get("serial")):
        # A device the survey has no row for (its VID:PID is a board vendor's or a firmware's
        # own) but whose USB serial names the unit: matched on that serial. Whether the same
        # serial shows in its bootloader and under other firmware is the board's to prove.
        match = {"usb_serial": str(usb["serial"])}
        note = "the device's own USB serial (kind unknown here: check it holds in every mode)"
    elif c.get("kind") == "uart-bridge":
        sys.exit(f"{name}: a board behind a {c.get('bridge') or 'USB-UART'} bridge with no serial: "
                 f"run the deep survey (`just survey {rec.get('host')} true`, which resets "
                 "an ESP32) so its MAC is read, then enroll it")
    else:
        sys.exit(f"{name}: the facts give no chip id, MAC or unit serial to match on; register "
                 "it by hand (a serial-less device can only be matched by VID:PID)")
    what = f"{usb.get('manufacturer', '?')} {usb.get('product', '?')}"
    if extra.get("model"):
        what = f"{extra['model']} ({what})"
    desc = (f"{what} -- enrolled {rec.get('last_seen', '')[:10]} from rxrbench survey facts on "
            f"{rec.get('host')}")
    if extra.get("delivered"):
        desc += f"; delivered with {extra['delivered']}"
    # A board shows a serial port in its application; one enrolled while in its bootloader (a
    # blank RP2350 shows only a drive) will have one too, and devreg maps a `serial` entry to
    # its port whenever it has one (a bare USB device until then).
    console = rec.get("serial_ports") or c.get("mode") == "bootloader"
    e = {"description": desc, "kind": "serial" if console else "usb", "destructive": True}
    if os.environ.get("RXRBENCH_OWNER"):
        e["owner"] = os.environ["RXRBENCH_OWNER"]
    if extra.get("alias"):
        e["aliases"] = [extra["alias"]]
    hw = {k: v for k, v in (("soc", tag.get("mcu")), ("psram", tag.get("psram")),
                            ("flash", tag.get("flash"))) if v}
    if hw:
        e["hardware"] = hw
    if extra.get("identity"):
        e["identity"] = extra["identity"]
    e["match"] = match
    types = board_types(rec, boards)
    if len(types) == 1:
        e["policy"] = {"board_tag": types[0]}
    if "usb_vid" in match:
        e.setdefault("policy", {})["chip_mac"] = str(probes["esptool"]["mac"]).lower()
        e["notes"] = ("no USB serial: matched by VID:PID, so a second board on a bridge of the "
                      "same make is ambiguous to devreg (give one a sibling_* match, or unplug "
                      "it); every esptool write first checks the chip's MAC (policy.chip_mac)")
    e["reach"] = {"usb": {"host": rec.get("host")}}
    return e, note


def is_probe_board(rec):
    """A debug probe the survey found (an ST-Link, a CMSIS-DAP): enrolled as a probe and the
    board behind it, never as a board of its own."""
    return (rec.get("class") or {}).get("kind") == "swd-probe"


def probe_entries(name, probe_name, rec, boards=None):
    """(the probe's entry, the board's entry) for a board behind its built-in debug probe, from
    the survey's facts, or exit with what is missing. Everything comes from what the survey
    read: the chip's unique id through the probe, the board type from the probe's drive."""
    c, probes, usb = rec.get("class") or {}, rec.get("probes") or {}, rec.get("usb") or {}
    swd = probes.get("swd") or {}
    host = rec.get("host")
    if not own_serial(usb.get("serial")):
        sys.exit(f"{name}: the probe has no USB serial of its own ({usb.get('serial') or 'none'}): "
                 "devreg could not tell it from another of its make, and nothing would say which "
                 "chip is wired to it. Register the probe and the chip by hand "
                 "(docs/REGISTRY.md, `kind: swd`), with the probe matched where it is plugged in")
    types = board_types(rec, boards)
    if len(types) != 1:
        sys.exit(f"{name}: " + (f"the probe's drive fits the board types {', '.join(types)}"
                                if types else
                                f"no board type claims the probe's drive "
                                f"({(probes.get('probe_drive') or {}).get('label') or 'no drive read'})")
                 + ": add `probe_drive` to the one it is (catalog/boards.yml), run `just survey "
                 f"{host}` again, then enroll. Nothing written")
    if swd.get("error") or not swd.get("uid"):
        sys.exit(f"{name}: the survey did not read the chip behind the probe"
                 + (f" ({swd['error']})" if swd.get("error") else
                    " (the board type names no `mcu`, or the probe was registered)")
                 + f": its unique id is the only proof of which chip a write reaches. Check the "
                 f"board is powered, then `just survey {host}` again. Nothing written")
    if swd.get("board_type") and swd["board_type"] != types[0]:
        sys.exit(f"{name}: the chip was read as a {swd['board_type']}, the drive names "
                 f"{types[0]}. Nothing written")
    when = rec.get("last_seen", "")[:10]
    drive = probes.get("probe_drive") or {}
    probe = {"description": (f"{c.get('probe') or 'debug probe'} built into {name} "
                             f"({usb.get('manufacturer', '?')} {usb.get('product', '?')}"
                             + (f", firmware {drive['firmware']}" if drive.get("firmware") else "")
                             + (f", drive {drive['label']}" if drive.get("label") else "")
                             + f") -- enrolled {when} from rxrbench survey facts on {host}"),
             "kind": "usb", "destructive": True,
             "match": {"usb_vid": str(usb["usb_vid"]), "usb_pid": str(usb["usb_pid"]),
                       "usb_serial": str(usb["serial"])},
             "reach": {"usb": {"host": host}}}
    board = {"description": (f"{types[0]} ({swd.get('chip')}, "
                             f"{int(swd['flash_size']) // 1024} KB flash) behind its built-in "
                             f"probe {probe_name} -- enrolled {when} from rxrbench survey facts "
                             f"on {host}" if swd.get("flash_size") else
                             f"{types[0]} behind its built-in probe {probe_name} -- enrolled "
                             f"{when} from rxrbench survey facts on {host}"),
             "kind": "swd", "destructive": True, "policy": {"board_tag": types[0]}}
    if os.environ.get("RXRBENCH_OWNER"):
        probe["owner"] = board["owner"] = os.environ["RXRBENCH_OWNER"]
    ports = [x for x in rec.get("serial_ports") or () if not x.startswith("/dev/")]
    reach = {}
    if len(ports) == 1:
        # the probe's own serial port carries the board's console (a Nucleo's USART3): the
        # board's entry is matched there, and its usb route can reach the console
        board["match"] = {"serial_by_id": ports[0]}
        reach["usb"] = {"host": host, "can": ["shell", "diag"]}
    reach["swd"] = {"host": host, "probe": probe_name, "chip": str(swd["chip"]),
                    "uid": str(swd["uid"]).lower(), "state": "live", "can": ["flash", "diag"]}
    board["reach"] = reach
    return probe, board


def same_make(devices, match):
    """Registry entries that also match by `match`'s VID:PID alone (no distinguishing key):
    with both boards plugged in, devreg can tell neither apart."""
    from ansible_collections.rxrbench.core.plugins.module_utils import hwsurvey  # noqa: E402
    out = []
    for n, e in (devices or {}).items():
        for m in [(e or {}).get("match") or {}] + list((e or {}).get("match_also") or []):
            if m and not hwsurvey.distinguishes(m) and \
                    str(m.get("usb_vid", "")).lower() == match["usb_vid"].lower() and \
                    str(m.get("usb_pid", "")).lower() == match["usb_pid"].lower():
                out.append(n)
                break
    return sorted(out)


def insert(text, name, entry, note):
    """The entry as YAML text, put at the end of the `devices:` map (before the next top-level
    key), so the rest of the file -- comments and all -- is untouched."""
    block = yaml.safe_dump({name: entry}, sort_keys=False, width=100, allow_unicode=True)
    block = "".join("  " + ln if ln.strip() else ln for ln in block.splitlines(True))
    if note and "usb_serial" in (entry.get("match") or {}):
        block = block.replace(f"{entry['match']['usb_serial']}\n",
                              f"{entry['match']['usb_serial']}   # {note}\n", 1)
    lines = text.splitlines(True)
    start = next(i for i, ln in enumerate(lines) if ln.startswith("devices:"))
    # `devices: {}` (a site with none yet) becomes a block map the entry can go under
    lines[start] = re.sub(r"^devices:\s*\{\s*\}", "devices:", lines[start])
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[A-Za-z_]", lines[i])),
               len(lines))
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "".join(lines[:end]) + "\n" + block + "".join(lines[end:])


def declared_firmware(a, rec, img, catalog):
    """(family, key, value, fw): the `firmware:` block `--layout` writes for the board, checked
    against the catalog before anything is written: the family named, else the one the survey
    found running; its key's value resolved by the family to a catalogued image (an image named,
    for a family with no key). Exits with what to give instead. How the image is written is the
    catalog's to say (its flasher): the layout takes it from there, so nothing here guesses it."""
    tags = rec.get("tags") or []
    fam = a.family or next((t[3:] for t in tags if t.startswith("fw:") and t[3:] in families.FAMILIES), None)
    if not fam:
        sys.exit(f"{a.name}: say which firmware the layout declares (--family)")
    key = families.of(fam)["identity_key"]
    if key:
        if a.key is None:
            sys.exit(f"{a.name}: {fam} images are told apart by {key}: give --key")
        value = int(a.key) if a.key.lstrip("-").isdigit() else a.key
        fw = {"family": fam, key: value}
        record, problems = families.plugin(fam).image(fw, catalog)
        if record is None:
            sys.exit(f"{a.name}: {fam} {key} {value!r}: " + "; ".join(problems or ["not in the catalog"])
                     + ". Nothing written")
        for p in problems:
            print(f"{a.name}: warning: {p} (the layout check will say it again)")
        return fam, key, value, fw
    if not a.image or img is None:
        sys.exit(f"{a.name}: {fam} images are not told apart by a key: give --family and "
                 "--image")
    return fam, key, a.image, {"family": fam, "image": a.image}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("ident")
    ap.add_argument("--layout")
    ap.add_argument("--family", help="the firmware the layout declares for it (a row of families)")
    ap.add_argument("--key", help="the value of what its family tells its images apart by "
                                   "(its identity key), for a family that has one")
    ap.add_argument("--image", help="the catalogued image the layout declares, for a family "
                                     "whose images are not told apart by a key (stock firmware)")
    ap.add_argument("--probe-name", help="for a board behind its built-in debug probe: the "
                                          "probe's registry name (default stlink-<name> for an "
                                          "ST-Link, probe-<name> otherwise)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    # Said before anything is written: a --layout that would be ignored, or that names no
    # layout, would otherwise leave the board registered and in no layout, with nothing said.
    keyless = a.family in families.FAMILIES and not families.of(a.family)["identity_key"]
    if a.layout and keyless and not a.image:
        sys.exit(f"{a.name}: {a.family} images are not told apart by a key: give --image (the "
                 "catalogued image its layout declares), or leave --layout out and declare the "
                 "board by hand (docs/GETTING-STARTED.md#a-first-real-board, step 3)")
    img = None
    if a.layout and keyless and a.image:
        # said before anything is written, as a layout that is not there is
        from ansible_collections.rxrbench.core.plugins.module_utils import layout as lay  # noqa: E402
        from ansible_collections.rxrbench.core.plugins.plugin_utils import registry as reg_  # noqa: E402
        img = lay.load_catalog(*reg_.catalog_dirs())["images"].get(a.image)
        if not img or img.get("family") != a.family:
            sys.exit(f"{a.name}: no catalogued {a.family} image {a.image!r} (catalog/firmware.yml)")
    if a.layout and a.key is None and not (keyless and a.image):
        sys.exit(f"{a.name}: --layout adds the board to a layout only with --key (the value its "
                 "family tells images apart by: a build class, say). Give it, or "
                 "leave --layout out and declare the board by hand (docs/GETTING-STARTED.md#a-first-real-board, step 3)")
    if a.layout:
        try:
            site.layout_path(a.layout)
        except FileNotFoundError:
            sys.exit(f"no layout {a.layout!r}: create it under {site.layout_dirs()[0]} first "
                     "(docs/GETTING-STARTED.md#a-first-real-board, step 3)")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", a.name):
        sys.exit(f"{a.name}: a name is lower case letters, digits and dashes")
    with open(REGISTRY) as fh:
        text = fh.read()
    reg = yaml.safe_load(text)
    devices = reg["devices"]
    taken = {n for n in devices} | {str(x) for e in devices.values()
                                    for x in ((e or {}).get("aliases") or [])}
    if a.name in taken:
        sys.exit(f"{a.name}: already a device name or alias in the registry")
    fname, rec = find_facts(a.ident)
    if rec.get("registered"):
        sys.exit(f"{a.ident}: already registered as {', '.join(rec['registered'])}")
    from ansible_collections.rxrbench.core.plugins.module_utils import layout  # noqa: E402
    from ansible_collections.rxrbench.core.plugins.plugin_utils import registry  # noqa: E402
    boards = layout.load_catalog(*registry.catalog_dirs())["boards"]
    entries = []   # (name, entry, note) in the order they are written
    if is_probe_board(rec):
        default = ("stlink-" if str((rec.get("class") or {}).get("probe") or "").startswith("stlink")
                   else "probe-") + a.name
        probe_name = a.probe_name or default
        if not re.fullmatch(r"[a-z][a-z0-9-]*", probe_name):
            sys.exit(f"{probe_name}: a name is lower case letters, digits and dashes")
        if probe_name in taken or probe_name == a.name:
            sys.exit(f"{probe_name}: already a device name or alias in the registry (or the "
                     "board's): give the probe another with --probe-name")
        probe, entry = probe_entries(a.name, probe_name, rec, boards)
        uid = entry["reach"]["swd"]["uid"]
        have = [n for n, e in devices.items()
                if str((((e or {}).get("reach") or {}).get("swd") or {}).get("uid") or "").lower() == uid]
        if have:
            sys.exit(f"{a.ident}: chip {uid} is already registered as {', '.join(have)}. Nothing written")
        entries = [(probe_name, probe, "the probe's own serial"), (a.name, entry, None)]
        note = None
    else:
        entry, note = entry_for(a.name, rec, boards)
        entries = [(a.name, entry, note)]
    declared = None
    if a.layout and (a.key is not None or a.image):
        declared = declared_firmware(a, rec, img, layout.load_catalog(*registry.catalog_dirs()))
    types = board_types(rec, boards)
    if len(types) == 1:
        print(f"{a.name}: board type {types[0]} (policy.board_tag, from what the survey read)")
    elif types:
        print(f"{a.name}: the facts fit the board types {', '.join(types)}: set policy.board_tag "
              "in its registry entry to the one it is")
    else:
        print(f"{a.name}: no board type fits the facts: set policy.board_tag in its registry "
              "entry (catalog/boards.yml lists the types; a site adds its own)")
    if "usb_vid" in (entry.get("match") or {}) and not is_probe_board(rec):
        others = same_make(devices, entry["match"])
        if others:
            print(f"{a.name}: warning: {', '.join(others)} also match "
                  f"{entry['match']['usb_vid']}:{entry['match']['usb_pid']} alone; devreg refuses "
                  "each while both are plugged in on one host")
    for alias in entry.get("aliases", []):
        if alias in taken:
            entry["aliases"].remove(alias)
    new_text = text
    for n, e, why in entries:
        new_text = insert(new_text, n, e, why)
    new = yaml.safe_load(new_text)
    names = [n for n, _e, _w in entries]
    if {k: v for k, v in new["devices"].items() if k not in names} != devices or \
            any(new["devices"].get(n) != e for n, e, _w in entries) or \
            {k: v for k, v in new.items() if k != "devices"} != {k: v for k, v in reg.items()
                                                                  if k != "devices"}:
        sys.exit("the edited registry differs from the original beyond the new entry: nothing "
                 "written")
    if a.dry_run:
        print(yaml.safe_dump({n: e for n, e, _w in entries}, sort_keys=False))
        return
    with open(REGISTRY + ".tmp", "w") as fh:
        fh.write(new_text)
    os.replace(REGISTRY + ".tmp", REGISTRY)
    print(f"{', '.join(names)}: registered ({REGISTRY})")
    rec["key"], rec["registered"] = a.name, sorted(names)
    with open(os.path.join(FACTS, a.name + ".yaml"), "w") as fh:
        fh.write(f"# rxrbench survey facts for {a.name} -- generated, do not edit "
                 "(hand-kept facts belong in registry.yaml)\n")
        yaml.safe_dump(rec, fh, sort_keys=False)
    os.remove(os.path.join(FACTS, fname))
    print(f"{a.name}: facts filed under its name (was {fname})")
    push_registry(reg)
    if a.layout and (a.key is not None or a.image):
        try:
            layout = site.layout_path(a.layout)
        except FileNotFoundError:
            sys.exit(f"no layout {a.layout!r}: create it under {site.layout_dirs()[0]} first")
        os.makedirs(os.path.join(layout, "host_vars"), exist_ok=True)
        inv = os.path.join(layout, "devreg.yml")
        with open(inv) as fh:
            inv_text = fh.read()
        if not re.search(rf"^  {a.name}:", inv_text, re.M):
            with open(inv, "a") as fh:
                fh.write(f"  {a.name}: {{}}\n")   # its host: wherever it is seen (location.py)
        # A board that already runs a family with a state line needs no first flash; one the
        # survey found running the declared firmware (`fw:<family>`) needs none either.
        tags = rec.get("tags") or []
        answers = any(t.startswith("fw:") and families.of(t[3:])["state_command"] for t in tags)
        fam, key, value, fw = declared
        if not key:
            answers = answers or f"fw:{fam}" in tags
        hv = os.path.join(layout, "host_vars", a.name + ".yml")
        if not os.path.exists(hv):
            head = f"---\n# {entry['description']}\n" + ("" if answers else
                "# Not running this firmware yet: allow the first flash (`just boot`, then `just apply`). It\n"
                "# only matters while the board does not run it.\nbench_first_flash: true\n")
            with open(hv, "w") as fh:
                fh.write(head + yaml.safe_dump(families.plugin(fam).enroll_hostvars(a.name, fw, rec),
                                               sort_keys=False, width=100))
        what = f"{key} {value}" if key else f"{fam} image {value}"
        print(f"{a.name}: added to layouts/{a.layout} ({what}); next: `just hosts-setup "
              f"{a.layout}`, then `just boot {a.layout} --limit {a.name}` (the plan; add confirm=apply to write), then `just apply "
              f"{a.layout} --limit {a.name}`")


if __name__ == "__main__":
    main()
