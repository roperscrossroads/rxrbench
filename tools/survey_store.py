#!/usr/bin/env python3
"""Merge one host's usb_survey records into the facts store, and say what was found.

    survey_store.py --host benchpi1 --facts-dir <facts>  < records.json

One YAML file per device, named by its registry name or, for a device not yet registered,
by what it is (`new-<vid><pid>-<chip id or MAC>`). Each probe's result is kept with its own
time, so a passive survey does not erase what an earlier deep one learned; a probe that ran
and failed records its error without dropping the last good answer. Tags are recomputed
from the merged record.

Prints one line per device, and for each new device a registry block to paste (the identity
that will tell it apart: its USB serial, which on these boards is a chip id or MAC).
"""
import argparse
import datetime
import json
import os
import sys

import yaml

import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import hwsurvey  # noqa: E402
from ansible_collections.rxrbench.core.plugins.plugin_utils import families


def load_boards():
    """The board types, merged as the plays see them (core's, each plugin's, the site's): a
    tag such as a built-in probe's board type is read against them."""
    from ansible_collections.rxrbench.core.plugins.module_utils import layout  # noqa: E402
    from ansible_collections.rxrbench.core.plugins.plugin_utils import registry  # noqa: E402
    return layout.load_catalog(*registry.catalog_dirs())["boards"]


def merge(old, rec, host, now, boards=None):
    out = dict(old or {})
    out.update({"key": rec["key"], "host": host, "last_seen": now, "usb": rec["usb"],
                "class": rec["class"], "registered": rec["registered"],
                "serial_ports": [p["id"] or p["path"] for p in rec.get("serial_ports", [])],
                "block": rec.get("block", [])})
    out.setdefault("first_seen", now)
    probes = dict(out.get("probes") or {})
    for name, result in (rec.get("probes") or {}).items():
        probes[name] = {**result, "gathered": now}
    for name, err in (rec.get("probe_errors") or {}).items():
        probes.setdefault(name, {})
        probes[name] = {**probes[name], "last_error": err, "last_error_at": now}
    # One firmware at a time: when the board answers as one family, what another said before
    # is history, not a fact about it now (and the other way round). Kept, dated, under
    # `history`.
    fresh = rec.get("probes") or {}
    if any(f in fresh for f in families.FAMILIES):
        for before in [f for f in families.FAMILIES if f in probes and f not in fresh]:
            out.setdefault("history", {})[before] = probes.pop(before)
    out["probes"] = probes
    out["tags"] = hwsurvey.tags(out, boards or {}, family_tags=families.survey_tags)
    return out


def describe(rec):
    c, pr = rec["class"], rec.get("probes") or {}
    what = c.get("probe") or c.get("bridge") or c.get("mcu") or c.get("kind")
    bits = [f"{what}/{c.get('mode') or '-'}"]
    for fam in families.FAMILIES:
        # only a family whose probe ran on this board and found it (an empty record is found,
        # with nothing more known); asked of every board, a family would describe them all
        line = families.plugin(fam).survey_describe(pr[fam] or {}) if fam in pr else ""
        if line:
            bits.append(line)
    bl = pr.get("bootloader")
    if bl and "version" in bl:
        bits.append(f"bootloader {bl['version']}")
    ids = c.get("ids") or {}
    if ids:
        bits.append(" ".join(f"{k}={v}" for k, v in ids.items()))
    face = c.get("unregistered_face")
    if face:
        bits.append("devreg does not see it in this mode: add to its registry entry "
                    f"match_also: [{{usb_vid: '{face['usb_vid']}', usb_pid: '{face['usb_pid']}', "
                    f"usb_serial: '{face['usb_serial']}'}}]")
    return "; ".join(bits)


def registry_block(rec):
    u, c = rec["usb"], rec["class"]
    match = {"usb_serial": u["serial"]} if u.get("serial") else \
        {"usb_vid": u["usb_vid"], "usb_pid": u["usb_pid"]}
    entry = {"description": f"{u.get('manufacturer', '?')} {u.get('product', '?')} "
                            f"({c.get('kind')}) -- name me",
             "kind": "serial" if rec.get("serial_ports") else "usb",
             "destructive": True, "match": match,
             "reach": {"usb": {"host": rec["host"]}}}
    if not u.get("serial"):
        entry["notes"] = ("no USB serial: matched by VID:PID alone, so a second identical "
                          "device makes devreg refuse as ambiguous")
    return yaml.safe_dump({"<name>": entry}, sort_keys=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--facts-dir", required=True)
    a = ap.parse_args()
    records = json.load(sys.stdin)
    os.makedirs(a.facts_dir, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = 0
    boards = load_boards()
    for rec in records:
        path = os.path.join(a.facts_dir, rec["key"] + ".yaml")
        old = None
        if os.path.exists(path):
            with open(path) as fh:
                old = yaml.safe_load(fh)
        # A device seen before it was registered has a `new-...` file. Once its chip id or MAC
        # is registered it is filed under its name: carry the old record over, then drop it.
        ident = ((rec.get("class") or {}).get("ids") or {})
        ident = ident.get("chip_id") or (ident.get("mac") or "").replace(":", "").lower()
        if rec["registered"] and ident:
            for stale in sorted(os.listdir(a.facts_dir)):
                if stale.startswith("new-") and stale.endswith(f"-{ident}.yaml"):
                    with open(os.path.join(a.facts_dir, stale)) as fh:
                        earlier = yaml.safe_load(fh) or {}
                    old = {**earlier, **(old or {}),
                           "probes": {**(earlier.get("probes") or {}),
                                      **((old or {}).get("probes") or {})}}
                    old.setdefault("first_seen", earlier.get("first_seen"))
                    os.remove(os.path.join(a.facts_dir, stale))
                    print(f"{rec['key']}: merged {stale} (seen before it was registered)")
        new = merge(old, rec, a.host, now, boards)
        with open(path + ".tmp", "w") as fh:
            fh.write(f"# rxrbench survey facts for {rec['key']} -- generated by `just survey`, "
                     "do not edit (hand-kept facts belong in registry.yaml)\n")
            yaml.safe_dump(new, fh, sort_keys=False)
        os.replace(path + ".tmp", path)
        written += 1
        shared = (rec.get("class") or {}).get("ambiguous") or {}
        reg = ",".join(rec["registered"]) or ("AMBIGUOUS:" + ",".join(shared) if shared else "NEW")
        if len(rec["registered"]) > 1:
            reg = "AMBIGUOUS:" + reg
        print(f"{rec['key']:<40} [{reg}] {describe(new)}")
        print(f"{'':<40} tags: {' '.join(new['tags'])}")
        for name, p in (new.get("probes") or {}).items():
            if p.get("last_error_at") == now:
                print(f"{'':<40} {name} probe failed: {p['last_error']}")
            elif p.get("gathered") == now and p.get("came_back") is False:
                print(f"{'':<40} {name} probe read, then: {p['came_back_note']}")
        for name, where in shared.items():
            print(f"{'':<40} '{name}' matches {len(where)} devices here ({', '.join(where)}): "
                  "neither is taken for it. Give its registry entry a key that tells them apart "
                  "(usb_serial, serial_by_id, a sibling_*), or unplug the other.")
        if not rec["registered"] and not shared and new["class"].get("kind") == "uart-bridge" \
                and not (new.get("probes") or {}).get("esptool", {}).get("mac"):
            print(f"{'':<40} a board behind a bridge: `just survey {a.host} true` reads its "
                  "chip and MAC (it resets an ESP32), which `just enroll` registers it by")
        if not rec["registered"] and not shared:
            for line in registry_block({**rec, "host": a.host}).splitlines():
                print(f"{'':<42}{line}")
    print(f"{written} device(s) written to {a.facts_dir}")


if __name__ == "__main__":
    main()
