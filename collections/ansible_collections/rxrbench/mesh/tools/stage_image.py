#!/usr/bin/env python3
"""stage_image.py <build-dir> --board-tag TAG [--store DIR] : a firmware build -> the image store.

Runs on the WORKSTATION. Turns a zephyrtastic sysbuild directory (MCUboot + app) into an
image directory the flashers take: the files to write and a manifest.json that says what the
image is. The identity comes from the build itself, never from an argument:

  class, version, hash   the app's signed MCUboot image (this plugin's host/bin/imginfo.py)
  build                  the build-id header the build generated (what the board will report)
  target                 CONFIG_BOARD

  ESP32 (esp-parts)  app.signed.bin and flash.json (offsets from this build's devicetree,
                     by tools/partmap.py); mcuboot.bin is kept beside them for the
                     record and is not written by the flasher
  nRF52 (uf2)        app.uf2: MCUboot + app from the merged hex, for the UF2 bootloader;
                     and app.signed.bin, the app alone, for an update over MCUmgr

The store key is <family>/class-<class>[-<feature>...]-<build>, so two builds never share a
directory, and a class built with a feature (a brain) is not mistaken for the plain class.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, *[".."] * 5))
sys.path.insert(0, os.path.join(REPO, "collections"))
from ansible_collections.rxrbench.core.plugins.module_utils import identity, layout, site  # noqa: E402
from ansible_collections.rxrbench.core.plugins.plugin_utils import registry  # noqa: E402
site.load_env()
try:
    WS = site.firmware_source("zephyrtastic")["workspace"]
except KeyError:
    WS = os.path.expanduser("~/zephyrtastic")
UF2_FAMILY_NRF52840 = "0xADA52840"


def run(*argv):
    r = subprocess.run(argv, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        sys.exit(f"{' '.join(argv[:3])} ...: rc {r.returncode}\n{r.stdout}{r.stderr}")
    return r.stdout


def config(app, key):
    with open(os.path.join(app, ".config")) as f:
        m = re.search(rf'^{key}="?([^"\n]*)"?$', f.read(), re.M)
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("build")
    ap.add_argument("--board-tag", required=True, help="the registry's policy.board_tag")
    ap.add_argument("--feature", action="append", default=[],
                    help="what the build has beyond its class recipe (e.g. brain); part of "
                         "the store key")
    ap.add_argument("--store", default=os.path.join(REPO, ".build", "store"))
    a = ap.parse_args()
    build = os.path.abspath(a.build)
    app = os.path.join(build, "meshtastic", "zephyr")
    signed = os.path.join(app, "zephyr.signed.bin")
    if not os.path.exists(signed):
        sys.exit(f"{signed}: not a signed sysbuild image")

    info = json.loads(run(sys.executable, os.path.join(HERE, "..", "host", "bin", "imginfo.py"), signed,
                          "--json"))
    with open(os.path.join(build, "meshtastic", "modules", "meshtastic-zephyr", "src",
                           "generated", "meshtastic_build_id.h")) as f:
        build_id = re.search(r'MESHTASTIC_BUILD_ID "([^"]+)"', f.read()).group(1)
    if build_id.endswith("-dirty"):
        sys.exit(f"the build is from a dirty tree ({build_id}): not staged")
    board = config(app, "CONFIG_BOARD")
    # The board tag is a board TYPE of the catalog (what the registry's policy.board_tag names),
    # not the Zephyr board the image was built for: a flash compares the two tags, so an image
    # staged under another word is refused on the board it was built for.
    kinds = layout.load_catalog(*registry.catalog_dirs())["boards"]
    if a.board_tag not in kinds:
        sys.exit(f"board type {a.board_tag!r} is not in catalog/boards.yml (or the site's); "
                 f"the types are: {', '.join(sorted(kinds))}")
    manifest = {"family": "zephyrtastic", "target": board, "board_tag": a.board_tag,
                "class": info["class"], "version": identity.mcuboot_version(info),
                "hash": info["sha256"],
                "build": build_id}
    if a.feature:
        manifest["features"] = sorted(a.feature)
    # How the build can be updated over MCUmgr (the smp flasher refuses a route the image cannot
    # answer on: a class 4 build is BLE only, and a serial plan would wait for nothing).
    with open(os.path.join(app, ".config")) as f:
        cfg = f.read()
    transports = [t for t, opt in (("serial", "CONFIG_MCUMGR_TRANSPORT_SHELL"),
                                   ("serial", "CONFIG_MCUMGR_TRANSPORT_UART"),
                                   ("ble", "CONFIG_MCUMGR_TRANSPORT_BT"),
                                   ("udp", "CONFIG_MCUMGR_TRANSPORT_UDP"))
                  if re.search(rf"^{opt}=y$", cfg, re.M)]
    manifest["mcumgr"] = sorted(set(transports))
    key = "-".join([f"class-{info['class']}", *sorted(a.feature), build_id])
    out = os.path.join(a.store, "zephyrtastic", key)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    # The application's ELF, for `just debug` and addr2line: the symbols of exactly this build.
    # Beside the store, not in it: the image directory is copied to a bench host at every flash.
    dbg = os.path.join(os.path.dirname(a.store), "debug", "zephyrtastic", key)
    os.makedirs(dbg, exist_ok=True)
    shutil.copy2(os.path.join(app, "zephyr.elf"), os.path.join(dbg, "zephyr.elf"))

    if config(app, "CONFIG_SOC_SERIES_ESP32S3") == "y":
        # What esptool is told the chip is: the board type's MCU (catalog/boards.yml).
        chip = (kinds.get(a.board_tag) or {}).get("mcu")
        if not chip:
            sys.exit(f"board type {a.board_tag!r} is not in catalog/boards.yml (or the site's): no chip to name")
        pm = json.loads(run(sys.executable, os.path.join(HERE, "partmap.py"),
                            os.path.join(app, "zephyr.dts"), "--json", "--require",
                            "FLASH_SIZE,SLOT0,SLOT1,STORAGE,STORAGE_SIZE"))["values"]
        shutil.copy2(os.path.join(build, "mcuboot", "zephyr", "zephyr.bin"),
                     os.path.join(out, "mcuboot.bin"))
        shutil.copy2(signed, os.path.join(out, "app.signed.bin"))
        flash = {
            "boot_mode": "mcuboot", "chip": chip, "board": board,
            "board_tag": a.board_tag, "class": info["class"],
            "flash_size": f"{int(pm['FLASH_SIZE'], 16) // 1048576}MB",
            "flash_freq": "80m", "flash_mode": "dio",
            "images": [{"offset": "0x0", "file": "mcuboot.bin", "role": "bootloader"},
                       {"offset": pm["SLOT0"], "file": "app.signed.bin", "role": "slot0"}],
            "partitions": {"slot0": pm["SLOT0"], "slot1": pm["SLOT1"], "storage": pm["STORAGE"],
                           "storage_size": pm["STORAGE_SIZE"]},
            "preserve": [{"offset": pm["STORAGE"], "size": pm["STORAGE_SIZE"],
                          "role": "storage_nvs"}],
            "meshtastic_zephyr_sha": build_id,
            # The whole map, as the build's devicetree has it: a backup reads from here.
            "map": pm,
        }
        with open(os.path.join(out, "flash.json"), "w") as f:
            json.dump(flash, f, indent=1)
        manifest.update(format="esp-parts", file="app.signed.bin")
    elif config(app, "CONFIG_SOC_NRF52840") == "y":
        merged = [f for f in os.listdir(build) if f.startswith("merged") and f.endswith(".hex")]
        if len(merged) != 1:
            sys.exit(f"{build}: expected one merged*.hex, found {merged}")
        run(sys.executable, os.path.join(WS, "zephyr", "scripts", "build", "uf2conv.py"), "-c",
            "-f", UF2_FAMILY_NRF52840, "-o", os.path.join(out, "app.uf2"),
            os.path.join(build, merged[0]))
        # The signed app by itself, for an update over MCUmgr (the UF2 holds MCUboot too).
        shutil.copy2(signed, os.path.join(out, "app.signed.bin"))
        manifest.update(format="uf2", file="app.uf2")
    else:
        sys.exit(f"{board}: neither an ESP32-S3 nor an nRF52840 build; no flasher for it")

    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print(f"{out}\n  {json.dumps(manifest)}\n  symbols: {dbg}/zephyr.elf")


if __name__ == "__main__":
    main()
