#!/usr/bin/env python3
"""Build a Zephyr application for native_sim and stage it as an image of the simulated bench.

    zephyr_sim.py build --name NAME --app APP [--workspace W] [--board B] [--conf FILE ...]

APP is an application directory (one from here, else relative to the workspace); W is a west
workspace holding a Zephyr (default: RXRBENCH_ZEPHYR_WORKSPACE); B is the board (default
native_sim/native/64). The shell goes on a pty of its own (core/sim/zephyr/pty.conf) and any
--conf is added after it. The result is `.build/store/zephyr/NAME/zephyr.exe` with a manifest
naming the `bare` family, the executable's hash and the Zephyr commit it was built from: what
a board of it is known by (flash/sim.py).

The tool brings no Zephyr: any workspace `west` can build in will do (its `.venv/bin/west`
is used when there is one, else the `west` on PATH).
"""
import argparse
import hashlib
import json
import struct
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "sim")
STORE = os.path.join(REPO, ".build", "store", "zephyr")
BUILDS = os.path.join(REPO, ".build", "zephyr")
BOARD = "native_sim/native/64"


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mcuboot_hash(path):
    """The SHA-256 an MCUboot image carries in its TLVs (type 0x10): what MCUmgr reports for
    the slot that holds it."""
    with open(path, "rb") as f:
        data = f.read()
    hdr_size, img_size = struct.unpack_from("<H", data, 8)[0], struct.unpack_from("<I", data, 12)[0]
    pos = hdr_size + img_size
    while pos + 4 <= len(data):
        magic, total = struct.unpack_from("<HH", data, pos)
        if magic not in (0x6907, 0x6908):       # TLV info: unprotected, protected
            break
        end, pos = pos + total, pos + 4
        while pos + 4 <= end:
            kind, length = struct.unpack_from("<BxH", data, pos)
            if kind == 0x10:
                return data[pos + 4:pos + 4 + length].hex()
            pos += 4 + length
    sys.exit(f"{path}: no SHA-256 TLV in the signed image")


def west_of(workspace):
    own = os.path.join(workspace, ".venv", "bin", "west")
    if os.access(own, os.X_OK):
        return own
    found = shutil.which("west")
    if not found:
        sys.exit(f"no west: none in {workspace}/.venv and none on PATH")
    return found


def zephyr_commit(workspace):
    r = subprocess.run(["git", "-C", os.path.join(workspace, "zephyr"), "rev-parse", "--short=8", "HEAD"],
                       capture_output=True, text=True, check=False)
    return r.stdout.strip() or "unknown"


def zephyr_version(workspace):
    """The workspace's Zephyr release as "MAJOR.MINOR.PATCH", from zephyr/VERSION, or None."""
    fields = {}
    try:
        with open(os.path.join(workspace, "zephyr", "VERSION")) as f:
            for line in f:
                k, _, v = line.partition("=")
                fields[k.strip()] = v.strip()
        return f"{int(fields['VERSION_MAJOR'])}.{int(fields['VERSION_MINOR'])}.{int(fields['PATCHLEVEL'])}"
    except (OSError, KeyError, ValueError):
        return None


def build(a):
    workspace = os.path.abspath(os.path.expanduser(a.workspace or os.environ.get("RXRBENCH_ZEPHYR_WORKSPACE") or ""))
    if not a.workspace and not os.environ.get("RXRBENCH_ZEPHYR_WORKSPACE"):
        sys.exit("no workspace: --workspace W, or RXRBENCH_ZEPHYR_WORKSPACE=W (a west workspace "
                 "holding a Zephyr)")
    if not os.path.isdir(os.path.join(workspace, "zephyr")):
        sys.exit(f"{workspace}: no zephyr/ in it (not a west workspace?)")
    # an application directory: as given when it is one from here (a plugin's own
    # application), else inside the workspace (one of Zephyr's samples)
    app = os.path.abspath(a.app) if os.path.isdir(a.app) else os.path.join(workspace, a.app)
    if not os.path.isfile(os.path.join(app, "CMakeLists.txt")):
        sys.exit(f"{app}: no CMakeLists.txt (not a Zephyr application?)")
    confs = [os.path.join(SIM, "zephyr", "pty.conf")] + [os.path.abspath(c) for c in a.conf]
    build_dir = os.path.join(BUILDS, a.name)
    env = {**os.environ, "ZEPHYR_TOOLCHAIN_VARIANT": os.environ.get("ZEPHYR_TOOLCHAIN_VARIANT", "host")}
    cmd = [west_of(workspace), "build", "-p", "auto", "-b", a.board, "-d", build_dir, app,
           "--", f"-DEXTRA_CONF_FILE={';'.join(confs)}"]
    if a.overlay:
        cmd.append("-DEXTRA_DTC_OVERLAY_FILE=" + ";".join(os.path.abspath(o) for o in a.overlay))
    print("+", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=workspace, env=env, check=False)
    if r.returncode != 0:
        sys.exit(f"west build failed (rc {r.returncode})")
    exe = os.path.join(build_dir, "zephyr", "zephyr.exe")
    if not os.access(exe, os.X_OK):
        sys.exit(f"{exe}: the build made no executable")
    out = os.path.join(STORE, a.name)
    os.makedirs(out, exist_ok=True)
    # replaced, not overwritten: a sim board may be running the store's executable right now
    shutil.copy2(exe, os.path.join(out, "zephyr.exe.new"))
    os.replace(os.path.join(out, "zephyr.exe.new"), os.path.join(out, "zephyr.exe"))
    manifest = {"family": a.family, "format": "sim-exe", "file": "zephyr.exe", "target": "native_sim",
                "board_tag": "native-sim", "app": os.path.relpath(app, workspace) if app.startswith(workspace) else app,
                "hash": sha256_of(exe), "build": zephyr_commit(workspace)}
    if a.sign:
        # An MCUboot image of the executable, for a board that takes images over MCUmgr: the
        # bytes a slot holds after an upload, with the hash MCUmgr reports for it. The sim
        # runs the executable (exe_hash is what it is known by); the signed file is what is
        # uploaded (hash is what the spare slot then reports).
        signed = os.path.join(out, "app.signed.bin")
        subprocess.run([os.path.join(os.path.dirname(sys.executable), "imgtool"), "sign",
                        "--header-size", "32", "--align", "4", "--version", a.sign, "--pad-header",
                        "--slot-size", a.slot_size, exe, signed], check=True)
        manifest.update(format="mcuboot-bin", file="app.signed.bin", exe="zephyr.exe",
                        exe_hash=manifest["hash"], hash=mcuboot_hash(signed), version=a.sign)
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f)
        f.write("\n")
    print(f"staged {out} (hash {manifest['hash'][:12]}, zephyr {manifest['build']})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build an application and stage it as an image")
    b.add_argument("--name", required=True, help="the image's name in the store")
    b.add_argument("--app", required=True, help="the application directory")
    b.add_argument("--workspace", help="the west workspace (default RXRBENCH_ZEPHYR_WORKSPACE)")
    b.add_argument("--board", default=BOARD)
    b.add_argument("--conf", action="append", default=[], help="an extra Kconfig fragment")
    b.add_argument("--overlay", action="append", default=[], help="an extra devicetree overlay")
    b.add_argument("--family", default="bare", help="the image's family (default bare)")
    b.add_argument("--sign", metavar="VERSION",
                   help="also stage an MCUboot image of the executable (imgtool), at this version")
    b.add_argument("--slot-size", default="0x600000",
                   help="the slot the signed image must fit (default: the big-flash overlay's)")
    a = ap.parse_args(argv)
    build(a)


if __name__ == "__main__":
    main()
