#!/usr/bin/env python3
"""Build U-Boot's sandbox binary, the one that runs the simulated A/B device's boot script
between its boots (sim/simvm.py), from mainline U-Boot at a pinned tag.

    uboot_sandbox.py build [--tag v2026.01] [--src DIR] [--out DIR] [--jobs N]

Clones the tag (shallow) into `--src` (default ~/.cache/rxrbench/simvm/uboot-src), builds
`sandbox_defconfig` without SDL, and puts `u-boot` and `u-boot.dtb` in `--out` (default
~/.cache/rxrbench/simvm/sandbox-built). Then `simvm.py setup --sandbox <out>`. Needs a host
gcc, make, bison, flex, libssl-dev, uuid-dev and python3-dev; swig comes from this tool's venv
(the linux collection's requirements), so nothing has to be installed as root for it. No
vendor tree: the boot script is plain U-Boot, and a stock sandbox runs it.
"""
import argparse
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))))
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "rxrbench", "simvm")
URL = "https://source.denx.de/u-boot/u-boot.git"
TAG = "v2026.01"


def build(a):
    src = os.path.abspath(os.path.expanduser(a.src))
    out = os.path.abspath(os.path.expanduser(a.out))
    if not os.path.isdir(os.path.join(src, ".git")):
        os.makedirs(os.path.dirname(src), exist_ok=True)
        print(f"+ clone {URL} at {a.tag} into {src}", flush=True)
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", a.tag, URL, src], check=True)
    else:
        got = subprocess.run(["git", "-C", src, "describe", "--tags", "--exact-match"],
                             capture_output=True, text=True, check=False).stdout.strip()
        if got != a.tag:
            sys.exit(f"{src} is at {got or 'no tag'}, not {a.tag}: remove it or give --src")
    env = {**os.environ, "PATH": os.path.join(REPO, "venv", "bin") + os.pathsep + os.environ.get("PATH", "")}
    for tool in ("gcc", "make", "bison", "flex", "swig"):
        if not shutil.which(tool, path=env["PATH"]):
            sys.exit(f"no {tool} on PATH (swig: `just setup` puts it in the tool's venv; the rest: apt)")
    print(f"+ make sandbox_defconfig in {src}", flush=True)
    subprocess.run(["make", "-s", "sandbox_defconfig"], cwd=src, env=env, check=True)
    # Two options reach for tools outside the build: the EFI capsule tool (gnutls) and capsule
    # authentication (efitools' signature lists). The boot script needs neither; off, and the
    # rest of the defconfig stays as it is (its unit tests and EFI are wired together).
    subprocess.run(["./scripts/config", "-d", "TOOLS_MKEFICAPSULE", "-d", "EFI_CAPSULE_AUTHENTICATE"],
                   cwd=src, env=env, check=True)
    subprocess.run(["make", "-s", "olddefconfig"], cwd=src, env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # the two files alone: the default target goes on to binman's packaging, which wants a
    # pylibfdt built for this very Python and gives the sandbox nothing it needs
    print(f"+ make -j{a.jobs} NO_SDL=1 u-boot u-boot.dtb", flush=True)
    log = os.path.join(src, "..", os.path.basename(src) + ".build.log")
    with open(log, "w") as f:
        r = subprocess.run(["make", f"-j{a.jobs}", "NO_SDL=1", "u-boot", "u-boot.dtb"], cwd=src, env=env,
                           stdout=f, stderr=subprocess.STDOUT, check=False)
    if r.returncode != 0:
        with open(log, errors="replace") as f:
            tail = f.read().splitlines()[-20:]
        sys.exit(f"make failed (rc {r.returncode}): see {log}; its last lines:\n" + "\n".join("  " + ln for ln in tail))
    os.makedirs(out, exist_ok=True)
    for n in ("u-boot", "u-boot.dtb"):
        shutil.copy2(os.path.join(src, n), os.path.join(out, n))
    print(f"built {out}/u-boot ({version_in(os.path.join(out, 'u-boot')) or 'version not found'}); "
          f"now: simvm.py setup --sandbox {out}")


def version_in(path):
    """The `U-Boot 20xx.yy` string a binary carries, or None."""
    import re  # pylint: disable=import-outside-toplevel
    with open(path, "rb") as f:
        m = re.search(rb"U-Boot (20\d\d\.\d\d(?:-rc\d)?)", f.read())
    return m.group(1).decode() if m else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="clone the tag, build the sandbox, put the two files in --out")
    b.add_argument("--tag", default=TAG)
    b.add_argument("--src", default=os.path.join(CACHE, "uboot-src"))
    b.add_argument("--out", default=os.path.join(CACHE, "sandbox-built"))
    b.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    a = ap.parse_args(argv)
    build(a)


if __name__ == "__main__":
    main()
