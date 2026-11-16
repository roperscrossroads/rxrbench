#!/usr/bin/env python3
"""zapp_build.py <board> <version> [--workspace W] [--module DIR]... [--mcuboot [--key PEM]] [--stage]: build
the zephyr plugin's example application, zapp, for a real board, and stage it in the image store
with what it provides.

<board> is a row of collections/ansible_collections/rxrbench/zephyr/sim/zapp/hw/boards.yml (its
Zephyr target, board type, confs, overlays and modules); <version> one of sim/zephyr/zapp-<v>.conf
(3.0, 3.1). The build is .build/zapp/<board>-<version>/; with --stage, its .uf2 goes to the store
as `zapp-<version>-<board>` (tools/stage_raw.py: the flash map from the build, the version, and
the features the build listed in zapp-features.txt), and the catalog entry for a site's
catalog/firmware.yml is printed.

--mcuboot builds it under MCUboot with sysbuild, for the board's `mcuboot.target`: MCUboot signed
with ECDSA P-256 (the key --key names; without it MCUboot's development key, public and only for
an example), zapp signed for it with MCUmgr over the shell's serial port and, with WiFi, over UDP.
Its version is the full x.y.0 MCUboot reports. Staged three ways: `zapp-<v>-<board>-mcuboot` (one
UF2, MCUboot and the signed app, family bare, for the first flash through picoboot),
`zapp-<v>-<board>-signed` (the signed app, family zephyr_settings, for an update over MCUmgr) and
`mcuboot-<board>` (the bootloader alone, for `just boot`), and their catalog entries printed.

W is a west workspace holding a Zephyr (default RXRBENCH_ZEPHYR_WORKSPACE); its `.venv/bin/west`
is used when there is one. A module a board needs that the workspace does not hold
(hal_rpi_pico, hal_infineon) is given with --module DIR, a directory named as the module.
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zephyr_sim  # noqa: E402  pylint: disable=wrong-import-position

ZAPP = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "zephyr", "sim", "zapp")
HW = os.path.join(ZAPP, "hw")
VERSIONS = os.path.join(os.path.dirname(ZAPP), "zephyr")
BUILDS = os.path.join(REPO, ".build", "zapp")


def boards():
    with open(os.path.join(HW, "boards.yml")) as f:
        return yaml.safe_load(f)


def module_dirs(workspace, names, extra):
    """Each named module's directory: the workspace's modules/hal/<name>, else the one of
    `extra` (the --module directories) named so."""
    out = []
    for name in names:
        if os.path.isdir(os.path.join(workspace, "modules", "hal", name.removeprefix("hal_"))):
            continue   # the workspace's own: west finds it
        found = [p for p in extra if os.path.basename(os.path.normpath(p)) == name]
        if not found:
            sys.exit(f"zapp_build: {name} is not in {workspace}/modules/hal: give its directory "
                     f"with --module DIR")
        out.append(os.path.abspath(os.path.expanduser(found[0])))
    return out


def command(west, row, version, build_dir, modules):
    """The west build line for one board and version."""
    confs = [os.path.join(HW, "hw.conf")] + [os.path.join(HW, c) for c in row.get("confs") or []] + \
        [os.path.join(VERSIONS, f"zapp-{version}.conf")]
    cmd = [west, "build", "-p", "always", "-b", row["target"], "-S", "cdc-acm-console", "-d", build_dir, ZAPP,
           "--", "-DEXTRA_CONF_FILE=" + ";".join(confs)]
    if row.get("overlays"):
        cmd.append("-DEXTRA_DTC_OVERLAY_FILE=" + ";".join(os.path.join(HW, o) for o in row["overlays"]))
    if modules:
        cmd.append("-DZEPHYR_EXTRA_MODULES=" + ";".join(modules))
    return cmd


def command_mcuboot(west, row, version, build_dir, modules, key=None):
    """The sysbuild line for a board under MCUboot: its mcuboot target, MCUboot signed ECDSA P-256
    (with `key`, else MCUboot's development key), zapp's confs, MCUmgr's, and the full version."""
    full = full_version(version)
    confs = [os.path.join(HW, "hw.conf")] + [os.path.join(HW, c) for c in row.get("confs") or []] + \
        [os.path.join(HW, "mcuboot.conf")] + \
        ([os.path.join(HW, "mcuboot-udp.conf")] if "wifi.conf" in (row.get("confs") or []) else []) + \
        [os.path.join(VERSIONS, f"zapp-{version}.conf")]
    mb = row["mcuboot"]
    cmd = [west, "build", "-p", "always", "--sysbuild", "-b", mb["target"], "-S", "cdc-acm-console", "-d", build_dir,
           ZAPP, "--", "-DSB_CONFIG_BOOTLOADER_MCUBOOT=y", "-DSB_CONFIG_BOOT_SIGNATURE_TYPE_ECDSA_P256=y",
           "-Dzapp_EXTRA_CONF_FILE=" + ";".join(confs),
           f'-Dzapp_CONFIG_ZAPP_VERSION="{full}"', f'-Dzapp_CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="{full}"']
    if key:
        cmd.append(f'-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="{os.path.abspath(os.path.expanduser(key))}"')
    if mb.get("overlays"):
        cmd.append("-Dzapp_EXTRA_DTC_OVERLAY_FILE=" + ";".join(os.path.join(HW, o) for o in mb["overlays"]))
    if modules:
        cmd.append("-DZEPHYR_EXTRA_MODULES=" + ";".join(modules))
    return cmd


def full_version(version):
    """x.y as MCUboot's header gives it: x.y.0."""
    return version if version.count(".") >= 2 else version + ".0"


UF2_MAGIC = (0x0A324655, 0x9E5D5157, 0x0AB16F30)


def uf2_family(path):
    """The family id of a UF2 file's first block."""
    with open(path, "rb") as f:
        head = f.read(32)
    return struct.unpack_from("<8I", head)[7]


def merged_uf2(hex_files, out, family):
    """One UF2 of the given Intel HEX files (overlap refused), in 256-byte blocks of the addresses
    they hold: what the picoboot flasher writes, MCUboot and the signed app at once."""
    from intelhex import IntelHex  # pylint: disable=import-outside-toplevel
    ih = IntelHex()
    ih.padding = 0xFF
    for h in hex_files:
        ih.merge(IntelHex(h), overlap="error")
    starts = sorted({a & ~0xFF for a in ih.addresses()})
    with open(out, "wb") as f:
        for n, at in enumerate(starts):
            data = bytes(ih.tobinarray(start=at, size=256))
            f.write(struct.pack("<8I", UF2_MAGIC[0], UF2_MAGIC[1], 0x2000, at, 256, n, len(starts), family)
                    + data.ljust(476, b"\0") + struct.pack("<I", UF2_MAGIC[2]))
    return len(starts)


def mcuboot_features(build_dir):
    """What a build under MCUboot provides: zapp's own list, and mcuboot and its MCUmgr transports."""
    with open(os.path.join(build_dir, "zapp", "zapp-features.txt")) as f:
        features = set(f.read().split())
    with open(os.path.join(build_dir, "zapp", "zephyr", ".config")) as f:
        cfg = f.read()
    features.add("mcuboot")
    if "CONFIG_MCUMGR_TRANSPORT_SHELL=y" in cfg:
        features.add("smp-serial")
    if "CONFIG_MCUMGR_TRANSPORT_UDP=y" in cfg:
        features.add("smp-udp")
    return sorted(features)


def stage_bootloader(build_dir, board, row, store):
    """<store>/bootloader/mcuboot-<board>/: mcuboot.bin, its manifest (its identity as bootid reads
    it: kind, signing key, length, hash) and the partitions (flash.json), for `just boot`."""
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import collections_path  # noqa: F401  pylint: disable=unused-import,import-outside-toplevel
    import stage_raw  # pylint: disable=import-outside-toplevel
    from ansible_collections.rxrbench.core.plugins.module_utils import bootid  # pylint: disable=import-outside-toplevel
    src = os.path.join(build_dir, "mcuboot", "zephyr", "zephyr.bin")
    with open(src, "rb") as f:
        data = f.read()
    ident = bootid.identify(data, family="raw")
    name = f"mcuboot-{board}"
    out = os.path.join(store, "bootloader", name)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    shutil.copy2(src, os.path.join(out, "mcuboot.bin"))
    manifest = {"family": "mcuboot", "format": "raw-bin", "file": "mcuboot.bin", "board_tag": row["board_tag"],
                "target": row["mcuboot"]["target"], "offset": "0x0", "length": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "key": ident.get("key"), "kind": ident.get("kind")}
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    with open(os.path.join(out, "flash.json"), "w") as f:
        json.dump({"map": stage_raw.flash_map(os.path.join(build_dir, "zapp")), "from": "zephyr.dts"}, f, indent=1)
    return name, manifest


def catalog_entry(name, row, version, features):
    return {name: {"family": "zephyr_settings",
                   "features": features,
                   "summary": f"zapp {version} ({' '.join(features)}), built by tools/zapp_build.py",
                   "board_tags": [row["board_tag"]], "flasher": "picoboot",
                   "source": {"type": "path", "path": f".build/store/zephyr_settings/{name}"}, "rf": "none"}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board")
    ap.add_argument("version")
    ap.add_argument("--workspace", help="the west workspace (default RXRBENCH_ZEPHYR_WORKSPACE)")
    ap.add_argument("--module", action="append", default=[], metavar="DIR",
                    help="a Zephyr module the workspace lacks (a directory named as the module)")
    ap.add_argument("--mcuboot", action="store_true", help="under MCUboot, with sysbuild (the board's mcuboot.target)")
    ap.add_argument("--key", help="with --mcuboot: MCUboot's signing key (ECDSA P-256 PEM); default its development key")
    ap.add_argument("--stage", action="store_true", help="stage the image and print its catalog entry")
    ap.add_argument("--store", default=os.path.join(REPO, ".build", "store"), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    table = boards()
    if a.board not in table:
        sys.exit(f"zapp_build: no board {a.board!r} in hw/boards.yml ({', '.join(table)})")
    if not os.path.isfile(os.path.join(VERSIONS, f"zapp-{a.version}.conf")):
        sys.exit(f"zapp_build: no sim/zephyr/zapp-{a.version}.conf")
    row = table[a.board]
    if a.mcuboot and not row.get("mcuboot"):
        sys.exit(f"zapp_build: {a.board} has no mcuboot target in hw/boards.yml")
    if a.key and not a.mcuboot:
        sys.exit("zapp_build: --key is for --mcuboot")
    workspace = os.path.abspath(os.path.expanduser(a.workspace or os.environ.get("RXRBENCH_ZEPHYR_WORKSPACE") or ""))
    if not os.path.isdir(os.path.join(workspace, "zephyr")):
        sys.exit("zapp_build: no workspace: --workspace W, or RXRBENCH_ZEPHYR_WORKSPACE=W (a west workspace)")
    modules = module_dirs(workspace, row.get("modules") or [], a.module)
    west = zephyr_sim.west_of(workspace)
    if a.mcuboot:
        return build_mcuboot(a, row, workspace, west, modules)
    build_dir = os.path.join(BUILDS, f"{a.board}-{a.version}")
    cmd = command(west, row, a.version, build_dir, modules)
    print("+", " ".join(cmd), flush=True)
    if subprocess.run(cmd, cwd=workspace, check=False).returncode:
        sys.exit("zapp_build: west build failed")
    uf2 = os.path.join(build_dir, "zephyr", "zephyr.uf2")
    with open(os.path.join(build_dir, "zapp-features.txt")) as f:
        features = f.read().split()
    print(f"zapp {a.version} for {a.board}: {uf2}; features {' '.join(features)}")
    if not a.stage:
        return 0
    name = f"zapp-{a.version}-{a.board}"
    stage = [sys.executable, os.path.join(REPO, "tools", "stage_raw.py"), uf2, "--name", name,
             "--board-tag", row["board_tag"], "--family", "zephyr_settings", "--target", row["target"],
             "--version", a.version, "--flash-map", build_dir, "--features", ",".join(features),
             "--store", a.store]
    if subprocess.run(stage, cwd=REPO, check=False).returncode:
        sys.exit("zapp_build: staging failed")
    print("# for the site's catalog/firmware.yml, under images:")
    print(yaml.safe_dump(catalog_entry(name, row, a.version, features), sort_keys=False, default_flow_style=None))
    return 0


def build_mcuboot(a, row, workspace, west, modules):
    """--mcuboot: the sysbuild, then (with --stage) the three stagings and their catalog entries."""
    build_dir = os.path.join(BUILDS, f"{a.board}-{a.version}-mcuboot")
    cmd = command_mcuboot(west, row, a.version, build_dir, modules, a.key)
    print("+", " ".join(cmd), flush=True)
    if subprocess.run(cmd, cwd=workspace, check=False).returncode:
        sys.exit("zapp_build: west build failed")
    app = os.path.join(build_dir, "zapp")
    signed = os.path.join(app, "zephyr", "zephyr.signed.bin")
    features = mcuboot_features(build_dir)
    print(f"zapp {full_version(a.version)} under MCUboot for {a.board}: {signed}; features {' '.join(features)}")
    if not a.stage:
        return 0
    merged = os.path.join(build_dir, "merged.uf2")
    merged_uf2([os.path.join(build_dir, "mcuboot", "zephyr", "zephyr.hex"),
                os.path.join(app, "zephyr", "zephyr.signed.hex")], merged,
               uf2_family(os.path.join(build_dir, "mcuboot", "zephyr", "zephyr.uf2")))
    base = f"zapp-{a.version}-{a.board}"
    steps = [[os.path.join(REPO, "tools", "stage_raw.py"), merged, "--name", f"{base}-mcuboot", "--board-tag",
              row["board_tag"], "--family", "bare", "--target", row["mcuboot"]["target"], "--version",
              full_version(a.version), "--flash-map", app, "--features", ",".join(features), "--store", a.store],
             [os.path.join(REPO, "tools", "stage_mcuboot.py"), signed, "--config", os.path.join(app, "zephyr", ".config"),
              "--name", f"{base}-signed", "--board-tag", row["board_tag"], "--family", "zephyr_settings",
              "--flash-map", app, "--features", ",".join(features), "--store", a.store]]
    for step in steps:
        if subprocess.run([sys.executable, *step], cwd=REPO, check=False).returncode:
            sys.exit("zapp_build: staging failed")
    boot_name, boot = stage_bootloader(build_dir, a.board, row, a.store)
    entries = {**catalog_entry(f"{base}-mcuboot", row, a.version, features),
               **catalog_entry(f"{base}-signed", row, a.version, list(features))}
    first, update = entries[f"{base}-mcuboot"], entries[f"{base}-signed"]
    first.update(family="bare", summary=f"MCUboot and zapp {full_version(a.version)} signed for it, one image: "
                 "the first flash through picoboot", source={"type": "path", "path": f".build/store/bare/{base}-mcuboot"})
    update.update(flasher="smp", summary=f"zapp {full_version(a.version)} signed for MCUboot: an update over MCUmgr",
                  source={"type": "path", "path": f".build/store/zephyr_settings/{base}-signed"})
    print("# for the site's catalog/firmware.yml, under images:")
    print(yaml.safe_dump(entries, sort_keys=False, default_flow_style=None))
    print(f"# its bootloader ({boot_name}: key {boot['key']}, {boot['length']} bytes), for bench_bootloader:")
    print(yaml.safe_dump({"bench_bootloader": {"name": boot_name, "sha256": boot["sha256"]}}, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
