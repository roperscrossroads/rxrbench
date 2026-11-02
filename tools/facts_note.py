#!/usr/bin/env python3
"""Record one probe's result in a device's facts file (what `just boot` learned, say).

    facts_note.py --facts-dir <facts> --key board1 --probe bootloader  < result.json
    facts_note.py --facts-dir <facts> --key board1 --probe bootloader --forget

The result is stored as `probes.<probe>` with the time, beside what `just survey` gathered,
and the tags are recomputed. A device with no facts file yet gets one. A result the facts
already hold is left as it is, its time included (`gathered`: when this result was first
read), so a run that learned nothing new changes no file and commits nothing.
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts-dir", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--probe", required=True)
    ap.add_argument("--forget", action="store_true",
                    help="drop the probe's result (a write made it stale); reads no stdin")
    a = ap.parse_args()
    path = os.path.join(a.facts_dir, a.key + ".yaml")
    rec = {"key": a.key}
    if os.path.exists(path):
        with open(path) as fh:
            rec = yaml.safe_load(fh) or rec
    if a.forget:
        if a.probe not in (rec.get("probes") or {}):
            print(f"{a.key}: no probes.{a.probe} to forget")
            return
        del rec["probes"][a.probe]
        rec["tags"] = hwsurvey.tags(rec, family_tags=families.survey_tags)
        write(path, a.key, rec)
        print(f"{a.key}: probes.{a.probe} forgotten")
        return
    result = json.load(sys.stdin)
    held = dict((rec.get("probes") or {}).get(a.probe) or {})
    held.pop("gathered", None)
    if os.path.exists(path) and held == result:
        print(f"{a.key}: probes.{a.probe} unchanged")
        return
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec.setdefault("probes", {})[a.probe] = {**result, "gathered": now}
    rec.setdefault("class", {})
    rec["tags"] = hwsurvey.tags(rec, family_tags=families.survey_tags)
    write(path, a.key, rec)
    print(f"{a.key}: probes.{a.probe} recorded")


def write(path, key, rec):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        fh.write(f"# rxrbench survey facts for {key} -- generated, do not edit "
                 "(hand-kept facts belong in registry.yaml)\n")
        yaml.safe_dump(rec, fh, sort_keys=False)
    os.replace(path + ".tmp", path)


if __name__ == "__main__":
    main()
