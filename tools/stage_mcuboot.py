#!/usr/bin/env python3
"""Stage a Zephyr application signed for MCUboot (built elsewhere, for hardware) into the image
store, for an update over MCUmgr (family zephyr_mcumgr, flasher smp).

    tools/stage_mcuboot.py <zephyr.signed.bin> --config <the app's .config> --name <key>
                           --board-tag <tag> [--family zephyr_mcumgr] [--store DIR]

What the image is comes from the image and its build, never from an argument:

  hash     the SHA-256 the signed image carries in its TLVs: what MCUmgr reports for the slot
           that holds it, so "does the device run this image" is one comparison
  version  the version in the MCUboot header, as the board reports it (1.0.0, or 1.0.0+3 when
           the build counter is set)
  target   CONFIG_BOARD
  mcumgr   the transports the build has (serial: the shell or a UART; ble; udp): the smp
           flasher refuses a route the image could never answer on

The result is <store>/<family>/<name>/ with app.signed.bin and manifest.json, which is what a
layout's image names. The bootloader is not staged here: it is written once, through a debug
probe or a ROM loader, as part of a whole-chip image (tools/stage_raw.py).
"""
import argparse
import json
import os
import re
import shutil
import struct
import sys

from zephyr_sim import mcuboot_hash  # noqa: E402  (tools/: the one TLV reader)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IMAGE_MAGIC = 0x96F3B83D
#: Kconfig option -> the transport name the smp flasher speaks of (as mesh's stage records it).
TRANSPORTS = (("serial", "CONFIG_MCUMGR_TRANSPORT_SHELL"), ("serial", "CONFIG_MCUMGR_TRANSPORT_UART"),
              ("ble", "CONFIG_MCUMGR_TRANSPORT_BT"), ("udp", "CONFIG_MCUMGR_TRANSPORT_UDP"))


def header_version(path):
    """'major.minor.revision+build' from the MCUboot image header, or exit when it is not one."""
    with open(path, "rb") as f:
        head = f.read(32)
    if len(head) < 32 or struct.unpack_from("<I", head, 0)[0] != IMAGE_MAGIC:
        sys.exit(f"stage_mcuboot: {path}: not an MCUboot image (no header magic): stage the signed "
                 "image (zephyr.signed.bin), not the plain one")
    major, minor, rev, build = struct.unpack_from("<BBHI", head, 20)
    # as a board running it says it (identity.mcuboot_version): the counter only when set
    return f"{major}.{minor}.{rev}" + (f"+{build}" if build else "")


def config_value(cfg, key):
    m = re.search(rf'^{key}="?([^"\n]*)"?$', cfg, re.M)
    return m.group(1) if m else None


def transports(cfg):
    return sorted({t for t, opt in TRANSPORTS if re.search(rf"^{opt}=y$", cfg, re.M)})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", help="the signed application (zephyr.signed.bin)")
    ap.add_argument("--config", required=True, help="the application's .config")
    ap.add_argument("--name", required=True)
    ap.add_argument("--board-tag", required=True)
    ap.add_argument("--family", default="zephyr_mcumgr")
    ap.add_argument("--store", default=os.path.join(REPO, ".build", "store"))
    a = ap.parse_args(argv)
    for path in (a.image, a.config):
        if not os.path.isfile(path):
            sys.exit(f"stage_mcuboot: {path}: no such file")
    version = header_version(a.image)
    with open(a.config) as f:
        cfg = f.read()
    manifest = {"family": a.family, "format": "mcuboot-bin", "file": "app.signed.bin",
                "board_tag": a.board_tag, "target": config_value(cfg, "CONFIG_BOARD"),
                "hash": mcuboot_hash(a.image), "version": version, "mcumgr": transports(cfg)}
    out = os.path.join(a.store, a.family, a.name)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    shutil.copy2(a.image, os.path.join(out, "app.signed.bin"))
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f)
        f.write("\n")
    print(out)
    print(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
