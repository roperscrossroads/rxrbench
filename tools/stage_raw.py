#!/usr/bin/env python3
"""Stage a raw flash image (a .bin linked for the start of flash, a .uf2, or an Intel HEX or ELF
file built elsewhere) into the image store.

    tools/stage_raw.py <image.bin|.uf2|.hex|.elf> --name <key> --board-tag <tag> [--target <board>]
                       [--family bare] [--version <v>] [--build <id>] [--flash-map <build dir>]
                       [--part <label>=<file>]... [--features a,b]

The result is <store>/<family>/<name>/ holding the file and a manifest.json, which is what a
layout's `bench_image` names. A raw image carries no version or build id of its own (give the
ones its firmware reports with --version and --build, when it reports them), so its
identity is a SHA-256: of the file for a .bin (the SWD flasher compares it with the hash of the
chip's flash), of the payload in address order for a .uf2 (the picoboot flasher reads the
chip's flash over the same addresses).

An Intel HEX file (.hex, .ihex) or an ELF file (told by its first bytes) is converted once, here: its pieces in
address order with the holes between them 0xFF, staged as `image.bin` with the lowest address
as the manifest's `base`, and the original beside it (`converted_from`: its name, format and
hash). Only a flasher that writes at an address takes such an image (the SWD flasher); the
others refuse it. A file that also writes far from its flash (an nRF52's UICR, an STM32's
option bytes) is refused: stage its flash part alone (module_utils/imagefile.py).

--flash-map names the Zephyr build the image came from: its devicetree (`zephyr/zephyr.dts`)
says where its shell answers (the manifest's `console`: usb, uart, none), and
says where each labelled partition is, and that goes beside the image as `flash.json`
(`{"map": {"STORAGE": "0x7e0000", "STORAGE_SIZE": "0x20000", ...}}`), which is how the
region level finds a board's config regions: from the build, never a guess.

--part lays an image a release publishes for one of its own partitions (a second app for the
OTA slot, a file system) into a merged ESP32 .bin, at the offset the merged file's partition
table gives that partition (named by its label, or by its subtype: `ota_1`, `spiffs`). The
staged file is then the chip as the release's full install leaves it, up to its last byte that
is not 0xFF (the flasher writes 0xFF after it); the files it was made of go beside it and the
manifest's `parts` says where each went.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import esppart, imagefile  # noqa: E402
from ansible_collections.rxrbench.core.plugins.module_utils.flash import picoboot  # noqa: E402


PARTITION = re.compile(r"(\w+)_partition:\s*partition@[0-9a-fA-F]+\s*\{(.*?)\};", re.S)
REG = re.compile(r"reg\s*=\s*<\s*(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s*>")


CHOSEN = re.compile(r"chosen\s*\{(.*?)\};", re.S)


def console_of(build_dir):
    """Where a Zephyr build's shell answers, from its devicetree: `usb` (a CDC-ACM UART, the
    board's own USB port), `uart` (a physical UART: on a board the bench reaches by USB, nothing
    answers there), `none`, or None when the build says nothing either way."""
    dts = os.path.join(build_dir, "zephyr", "zephyr.dts")
    if not os.path.exists(dts):
        return None
    with open(dts) as f:
        text = f.read()
    chosen = CHOSEN.search(text)
    if not chosen:
        return None
    m = re.search(r"zephyr,shell-uart\s*=\s*&(\w+)", chosen.group(1)) or \
        re.search(r"zephyr,console\s*=\s*&(\w+)", chosen.group(1))
    if not m:
        return "none"
    node = re.search(rf"\b{re.escape(m.group(1))}\s*:\s*[\w@-]+\s*\{{(.*?)\}};", text, re.S)
    compat = re.search(r'compatible\s*=\s*"([^"]+)"', node.group(1)) if node else None
    return "usb" if compat and "cdc-acm" in compat.group(1) else "uart"


def flash_map(build_dir):
    """{LABEL: offset, LABEL_SIZE: size} (hex strings) of every labelled partition in a Zephyr
    build's devicetree."""
    dts = os.path.join(build_dir, "zephyr", "zephyr.dts")
    with open(dts) as f:
        text = f.read()
    out = {}
    for label, body in PARTITION.findall(text):
        m = REG.search(body)
        if m:
            out[label.upper()] = m.group(1)
            out[label.upper() + "_SIZE"] = m.group(2)
    if not out:
        sys.exit(f"stage_raw: {dts} names no partitions")
    return out


def compose(merged, parts):
    """The merged image with each (label, file) laid into its partition, trailing 0xFF cut.
    Returns (bytes, [{label, offset, file, size}])."""
    with open(merged, "rb") as f:
        blob = bytearray(f.read())
    rows = esppart.parse(bytes(blob), esppart.OFFSET)
    if not rows:
        sys.exit(f"stage_raw: {merged} has no partition table at 0x8000: --part needs a merged image")
    placed = []
    for label, path in parts:
        row = next((r for r in rows if r["label"] == label), None) or next(
            (r for r in rows if esppart.subtype_name(r["type"], r["subtype"]) == label), None)
        if row is None:
            sys.exit(f"stage_raw: the image's partition table has no partition {label!r} ("
                     + ", ".join(r["label"] for r in rows) + ")")
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > row["size"]:
            sys.exit(f"stage_raw: {path} is {len(data)} B, partition {row['label']} only {row['size']}")
        off = row["offset"]
        if len(blob) > off and any(b != 0xFF for b in blob[off:off + row["size"]]):
            sys.exit(f"stage_raw: the merged image already fills partition {row['label']}")
        blob.extend(b"\xff" * max(0, off + len(data) - len(blob)))
        blob[off:off + len(data)] = data
        placed.append({"label": row["label"], "offset": f"{off:#x}",
                       "file": os.path.basename(path), "size": len(data)})
    return bytes(blob).rstrip(b"\xff"), placed



def board_type(tag):
    """The board type `tag` names (core's, the plugins' and the site's catalogs), or {}."""
    from ansible_collections.rxrbench.core.plugins.module_utils import layout  # noqa: E402
    from ansible_collections.rxrbench.core.plugins.plugin_utils import registry  # noqa: E402
    try:
        return (layout.load_catalog(*registry.catalog_dirs())["boards"] or {}).get(tag) or {}
    except (OSError, ValueError, KeyError):
        return {}

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image")
    ap.add_argument("--name", required=True, help="the image's key in the store")
    ap.add_argument("--board-tag", required=True, help="the registry's policy.board_tag")
    ap.add_argument("--target", help="what it was built for (a Zephyr board name)")
    ap.add_argument("--family", default="bare")
    ap.add_argument("--version")
    ap.add_argument("--build", help="the build id the firmware reports of itself (a release's "
                    "commit), for a family whose boards say it but cannot be hashed")
    ap.add_argument("--flash-map", help="the Zephyr build directory the image came from: its "
                    "devicetree's partitions go beside the image as flash.json")
    ap.add_argument("--part", action="append", default=[], metavar="LABEL=FILE",
                    help="an image of one of the merged .bin's partitions, laid in at its offset")
    ap.add_argument("--features", default="", help="what the image provides, comma-separated (a "
                    "catalog entry's `features`; the board exercise's second image must have them)")
    ap.add_argument("--store", default=os.path.join(REPO, ".build", "store"))
    a = ap.parse_args()
    if not os.path.isfile(a.image):
        sys.exit(f"stage_raw: {a.image}: no such file")
    uf2 = a.image.lower().endswith(".uf2")
    built = None if uf2 else imagefile.kind_of(a.image)
    if built == "ihex" and not a.image.lower().endswith((".hex", ".ihex")):
        built = None        # a raw .bin whose first byte happens to be ':'
    parts = [tuple(x.split("=", 1)) for x in a.part]
    if (uf2 or built) and parts:
        sys.exit("stage_raw: --part is for a merged ESP32 .bin")
    if any(len(x) != 2 or not x[0] or not os.path.isfile(x[1]) for x in parts):
        sys.exit("stage_raw: --part takes LABEL=FILE, the file existing")
    if uf2:
        try:
            blocks, fams = picoboot.uf2_blocks(a.image)
        except picoboot.Uf2Error as e:
            sys.exit(f"stage_raw: {e}")
        digest = picoboot.payload_hash(blocks)
    elif built:
        try:
            whole, base, _spans = imagefile.flatten(a.image, built,
                                                    flash=imagefile.flash_area(board_type(a.board_tag)))
        except (imagefile.ImageFileError, OSError, UnicodeDecodeError) as e:
            sys.exit(f"stage_raw: {e}")
        digest = hashlib.sha256(whole).hexdigest()
    elif parts:
        whole, placed = compose(a.image, parts)
        digest = hashlib.sha256(whole).hexdigest()
    else:
        with open(a.image, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    out = os.path.join(a.store, a.family, a.name)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    file = os.path.basename(a.image)
    shutil.copy2(a.image, os.path.join(out, file))
    if parts:
        for _label, path in parts:
            shutil.copy2(path, os.path.join(out, os.path.basename(path)))
        file = "chip.bin"
        with open(os.path.join(out, file), "wb") as f:
            f.write(whole)
    original = file
    if built:
        file = "image.bin" if original != "image.bin" else "image.raw.bin"
        with open(os.path.join(out, file), "wb") as f:
            f.write(whole)
    manifest = {"family": a.family, "format": "uf2" if uf2 else "raw-bin", "file": file,
                "board_tag": a.board_tag, "hash": digest}
    if built:
        with open(a.image, "rb") as f:
            manifest["base"] = f"{base:#x}"
            manifest["converted_from"] = {"file": original, "format": built,
                                          "sha256": hashlib.sha256(f.read()).hexdigest()}
    if parts:
        manifest["parts"] = [{"label": "merged", "offset": "0x0",
                              "file": os.path.basename(a.image)}] + placed
    if uf2:
        manifest["uf2_families"] = sorted(f"0x{f:08x}" for f in fams)
        manifest["writes"] = [[f"0x{lo:x}", f"0x{hi:x}"] for lo, hi in picoboot.ranges(blocks)]
    for key in ("target", "version", "build"):
        if getattr(a, key):
            manifest[key] = getattr(a, key)
    if a.flash_map and console_of(a.flash_map):
        # what the picoboot flasher checks before it writes a board reached by its console
        manifest["console"] = console_of(a.flash_map)
    if a.features:
        manifest["features"] = sorted(f for f in a.features.split(",") if f)
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    if a.flash_map:
        with open(os.path.join(out, "flash.json"), "w") as f:
            json.dump({"map": flash_map(a.flash_map), "from": "zephyr.dts"}, f, indent=1)
    print(f"{out}\n  {json.dumps(manifest)}")


if __name__ == "__main__":
    main()
