#!/usr/bin/env python3
"""Build a Zephyr application with MCUboot for an emulated MCU and stage it as an image of
the PC bench (docs/SIMULATED.md.

    zephyr_qemu.py build --name NAME --app APP [--workspace W] [--conf F ...]
                         [--board mps2/an385] [--overlay O ...] [--mcuboot-conf F]

`west build --sysbuild` with MCUboot in RAM-load mode with revert and ECDSA signing, the
overlay that gives the board a simulated flash (core/sim/zephyr/mps2-ramload.overlay) on both
images and the one that keeps MCUboot's RAM apart from the application's (the bootloader
copies the chosen image to RAM before validating it). Staged under `.build/store/zephyr/NAME/`:
`mcuboot.elf` (what QEMU boots, its simulated-flash segment removed so QEMU may place the
application there), `app.signed.bin` (what MCUmgr uploads: signed, no trailer, so an upload
is a candidate until it is confirmed), `factory.signed.bin` (the same image padded to the
slot with a confirmed trailer: what QEMU's loader places in slot 0, as a programmer would;
in revert mode MCUboot erases an image it finds unconfirmed after a boot), and a manifest:
family zephyr_mcumgr, format mcuboot-bin, the image's hash (its SHA-256 TLV, what MCUmgr
reports for the slot) and a `qemu` block the simulated board runs the machine by
(core/sim/simboard.py, console `qemu`). The SDK's objcopy is taken from PATH or
ZEPHYR_SDK_INSTALL_DIR.

What MCUboot does on this board, in this mode: at every boot it validates the highest
version it finds in the two slots and runs that; an image that was booted but not confirmed
is erased at the next boot (the revert); a lower version uploaded beside a higher one is
never booted. The bench's SMP flasher uploads, marks for test, resets and confirms, which
is the mode's upgrade; its downgrade is refused by the bootloader and the flasher says so.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zephyr_sim import SIM, STORE, BUILDS, mcuboot_hash, west_of, zephyr_commit, zephyr_version  # noqa: E402

BOARDS = {
    # board: (QEMU machine arguments, the simulated flash's slot 0 address, the overlay both
    # images carry, the overlay and the conf MCUboot alone carries: its own RAM apart from
    # the application's, and the retained region it tells the application its slot through)
    "mps2/an385": (["-machine", "mps2-an385", "-cpu", "cortex-m3"], "0x20310000",
                   "mps2-ramload.overlay", "mps2-ramload-mcuboot.overlay", "mps2-ramload-mcuboot.conf"),
}
# Where RAM load copies the chosen image: the `image_ram` node of the MCUboot overlay. MCUboot
# reads it from the chosen node `mcuboot,image-ram` (Zephyr 4.5's MCUboot); an older MCUboot
# (Zephyr 4.4's) only from two Kconfig options, which otherwise default to MCUboot's own RAM,
# where the image does not fit and MCUboot stops without a word.
IMAGE_RAM = {"mps2/an385": ("0x20000000", 0x002E0000)}


def mcuboot_knows_image_ram(workspace):
    """Whether the workspace's MCUboot takes the RAM-load area from `mcuboot,image-ram`."""
    kconfig = os.path.join(workspace, "bootloader", "mcuboot", "boot", "zephyr", "Kconfig")
    try:
        with open(kconfig) as f:
            return "mcuboot,image-ram" in f.read()
    except OSError:
        return True     # no MCUboot to read: leave its configuration as the overlay says


def build(a):
    workspace = os.path.abspath(os.path.expanduser(a.workspace or os.environ.get("RXRBENCH_ZEPHYR_WORKSPACE") or ""))
    if not a.workspace and not os.environ.get("RXRBENCH_ZEPHYR_WORKSPACE"):
        sys.exit("no workspace: --workspace W, or RXRBENCH_ZEPHYR_WORKSPACE=W")
    if a.board not in BOARDS:
        sys.exit(f"{a.board}: no emulated board of that name here (known: {', '.join(BOARDS)})")
    machine, slot0, overlay, boot_overlay, boot_conf = BOARDS[a.board]
    app = os.path.abspath(a.app) if os.path.isdir(a.app) else os.path.join(workspace, a.app)
    confs = [os.path.abspath(c) for c in a.conf]
    overlays = [os.path.join(SIM, "zephyr", overlay)] + [os.path.abspath(o) for o in a.overlay]
    boot_overlays = [os.path.join(SIM, "zephyr", overlay), os.path.join(SIM, "zephyr", boot_overlay)]
    mcuboot_conf = ";".join([os.path.abspath(a.mcuboot_conf or os.path.join(SIM, "zephyr", "mcuboot-small.conf")),
                             os.path.join(SIM, "zephyr", boot_conf)])
    build_dir = os.path.join(BUILDS, a.name)
    env = {**os.environ, "ZEPHYR_TOOLCHAIN_VARIANT": os.environ.get("ZEPHYR_TOOLCHAIN_VARIANT", "zephyr")}
    cmd = [west_of(workspace), "build", "-p", "always", "-b", a.board, "--sysbuild", "-d", build_dir, app, "--",
           "-DSB_CONFIG_BOOTLOADER_MCUBOOT=y", "-DSB_CONFIG_MCUBOOT_MODE_RAM_LOAD_WITH_REVERT=y",
           "-DSB_CONFIG_BOOT_SIGNATURE_TYPE_ECDSA_P256=y",
           f"-DEXTRA_CONF_FILE={';'.join(confs)}", f"-DEXTRA_DTC_OVERLAY_FILE={';'.join(overlays)}",
           f"-Dmcuboot_EXTRA_CONF_FILE={mcuboot_conf}", f"-Dmcuboot_EXTRA_DTC_OVERLAY_FILE={';'.join(boot_overlays)}",
           "-Dmcuboot_CONFIG_FLASH_SIMULATOR=y", "-DCONFIG_FLASH_SIMULATOR=y",
           # the padded, confirmed image QEMU loads into slot 0: Zephyr 4.5 implies it in this
           # mode, 4.4 does not, so it is asked for
           "-DCONFIG_MCUBOOT_GENERATE_CONFIRMED_IMAGE=y",
           # the version in the image's header: what MCUboot ranks the slots by in this mode
           f'-DCONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="{a.version}"']
    if a.board in IMAGE_RAM and not mcuboot_knows_image_ram(workspace):
        start, size = IMAGE_RAM[a.board]
        cmd += [f"-Dmcuboot_CONFIG_BOOT_IMAGE_EXECUTABLE_RAM_START={start}",
                f"-Dmcuboot_CONFIG_BOOT_IMAGE_EXECUTABLE_RAM_SIZE={size}"]
    print("+", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=workspace, env=env, check=False)
    if r.returncode != 0:
        sys.exit(f"west build failed (rc {r.returncode})")
    signed = os.path.join(build_dir, os.path.basename(app), "zephyr", "zephyr.signed.bin")
    confirmed = os.path.join(build_dir, os.path.basename(app), "zephyr", "zephyr.signed.confirmed.bin")
    elf = os.path.join(build_dir, "mcuboot", "zephyr", "zephyr.elf")
    for needed in (signed, confirmed, elf):
        if not os.path.isfile(needed):
            sys.exit(f"the build made no {needed}")
    out = os.path.join(STORE, a.name)
    os.makedirs(out, exist_ok=True)
    shutil.copy2(signed, os.path.join(out, "app.signed.bin"))
    shutil.copy2(confirmed, os.path.join(out, "factory.signed.bin"))
    # the SDK's objcopy: on PATH, or under ZEPHYR_SDK_INSTALL_DIR (where west found the SDK),
    # in either layout the SDK has had (gnu/<target>/bin since 1.0, <target>/bin before)
    objcopy = shutil.which("arm-zephyr-eabi-objcopy")
    sdk = os.environ.get("ZEPHYR_SDK_INSTALL_DIR")
    if not objcopy and sdk:
        for sub in (("gnu", "arm-zephyr-eabi"), ("arm-zephyr-eabi",)):
            candidate = os.path.join(sdk, *sub, "bin", "arm-zephyr-eabi-objcopy")
            if os.access(candidate, os.X_OK):
                objcopy = candidate
                break
    if not objcopy or not os.access(objcopy, os.X_OK):
        sys.exit("no arm-zephyr-eabi-objcopy: put the SDK's bin on PATH or set ZEPHYR_SDK_INSTALL_DIR")
    subprocess.run([objcopy, "-R", "FLASH_SIM", elf, os.path.join(out, "mcuboot.elf")], check=True)
    manifest = {"family": a.family, "format": "mcuboot-bin", "file": "app.signed.bin", "target": a.board,
                "board_tag": a.board_tag, "app": os.path.relpath(app, workspace) if app.startswith(workspace) else app,
                "hash": mcuboot_hash(os.path.join(out, "app.signed.bin")), "version": a.version,
                "build": zephyr_commit(workspace), "zephyr": zephyr_version(workspace),
                "qemu": {"system": "qemu-system-arm", "argv": machine, "kernel": "mcuboot.elf",
                         "load": [{"file": "factory.signed.bin", "addr": slot0}]}}
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f)
        f.write("\n")
    print(f"staged {out} (hash {manifest['hash'][:12]}, zephyr {manifest['build']})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build with MCUboot and stage")
    b.add_argument("--name", required=True)
    b.add_argument("--app", required=True)
    b.add_argument("--workspace")
    b.add_argument("--board", default="mps2/an385")
    b.add_argument("--board-tag", default="mps2-an385")
    b.add_argument("--conf", action="append", default=[])
    b.add_argument("--overlay", action="append", default=[])
    b.add_argument("--mcuboot-conf")
    b.add_argument("--family", default="zephyr_mcumgr")
    b.add_argument("--version", default="0.0.0")
    a = ap.parse_args(argv)
    build(a)


if __name__ == "__main__":
    main()
