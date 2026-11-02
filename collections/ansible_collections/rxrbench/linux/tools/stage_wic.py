#!/usr/bin/env python3
"""stage_wic.py <image.wic> --board-tag TAG [--family F] [--store DIR] : a Yocto disk image -> the image store.

Runs on the WORKSTATION, without root. A .wic says nothing about itself from outside, so
this writes the manifest the card writer needs, from the image's own contents:

  target    the RAUC `compatible`, read out of the first Linux partition's
            /usr/lib/rauc/system.conf (never retyped: a mistyped one looks like a signature
            failure on the device)
  version   the build stamp in the file name, as bitbake wrote it
  hash      the SHA-256 of the image file

The store entry links to the .wic and its .bmap (they are large) and holds manifest.json.
A .wic without a .bmap beside it is refused: the card writer would have no per-block
checksums to verify against.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 5))


def out_of(*argv):
    r = subprocess.run(argv, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        sys.exit(f"{argv[0]}: rc {r.returncode}: {r.stderr.strip()[-300:]}")
    return r.stdout


def compatible_of(wic):
    table = json.loads(out_of("/sbin/sfdisk", "-J", wic))["partitiontable"]
    sector = table.get("sectorsize", 512)
    for part in table["partitions"]:
        if part.get("type") not in ("83", "0FC63DAF-8483-4772-8E79-3D69D8477DE4"):
            continue
        r = subprocess.run(["/sbin/debugfs", "-R", "cat /usr/lib/rauc/system.conf",
                            f"{wic}?offset={part['start'] * sector}"],
                           capture_output=True, text=True, check=False)
        m = re.search(r"^compatible=(\S+)$", r.stdout, re.M)
        if m:
            return m.group(1)
    sys.exit(f"{wic}: no Linux partition with a RAUC system.conf: is this a RAUC image?")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wic")
    ap.add_argument("--board-tag", required=True, help="the registry's policy.board_tag")
    ap.add_argument("--family", default="linux_rauc",
                    help="the family the image is of (linux_rauc, linux_slotguard)")
    ap.add_argument("--store", default=os.path.join(REPO, ".build", "store"))
    a = ap.parse_args()
    wic = os.path.realpath(a.wic)
    if not os.path.isfile(wic + ".bmap"):
        sys.exit(f"{wic}.bmap is missing: not staged")
    stamp = re.search(r"rootfs-(\d{14})", os.path.basename(wic))
    digest = hashlib.sha256()
    with open(wic, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    manifest = {"family": a.family, "format": "wic", "file": "image.wic",
                "target": compatible_of(wic), "board_tag": a.board_tag,
                "version": stamp.group(1) if stamp else None, "hash": digest.hexdigest(),
                "source": wic}
    out = os.path.join(a.store, a.family, f"card-{manifest['version'] or digest.hexdigest()[:12]}")
    os.makedirs(out, exist_ok=True)
    for name, src in (("image.wic", wic), ("image.wic.bmap", wic + ".bmap")):
        link = os.path.join(out, name)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(src, link)
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print(f"{out}\n  {json.dumps({k: v for k, v in manifest.items() if k != 'source'})}")


if __name__ == "__main__":
    main()
