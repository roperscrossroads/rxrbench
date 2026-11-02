#!/usr/bin/env python3
"""What this machine has of what the simulated benches need 
(docs/GETTING-STARTED.md#the-simulated-benches).

    doctor.py [--require BENCH[,BENCH...]] [--json]

Benches: `containers` (the simulated Linux devices and stock Meshtastic nodes, in Docker),
`zephyr` (building the simulated Zephyr boards), `qemu` (the emulated MCU with
MCUboot), `nuttx` (NuttX's sim board), `smp` (the MCUmgr client venv the SMP flasher runs
under, needed by the sim tests), `vmhost` (the simulated bench host), `ab` (the simulated
A/B Linux device), `secrets` (the encrypted store the secrets tests use); `workstation` is
always checked. Each line says whether a prerequisite is there and, when not, what provides
it. The images a bench builds are reported as built or not, and never required: they are
what the recipes make, not what the machine must have. `--require` makes the exit code say
whether those benches can run; CI requires the ones it builds, so a new prerequisite has to
be added here to pass. Settings come from the environment, or the repository's `.env`.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402  pylint: disable=wrong-import-position
from ansible_collections.rxrbench.core.sim import vmhost  # noqa: E402  pylint: disable=wrong-import-position

STORE = os.path.join(REPO, ".build", "store")
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "rxrbench")
MCUMGR_PY = os.path.expanduser("~/.venv-mcumgr/bin/python")


def version_of(cmd, pattern=r"(\d+)\.(\d+)(?:\.(\d+))?"):
    """The first version in a command's `--version` output, as a tuple, or None."""
    exe = shutil.which(cmd[0]) if not os.path.isabs(cmd[0]) else cmd[0]
    if not exe or not os.access(exe, os.X_OK):
        return None
    try:
        out = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(pattern, out.stdout + out.stderr)
    return tuple(int(x or 0) for x in m.groups()) if m else None


def on_path(name, hint):
    p = shutil.which(name)
    return (p is not None, name, p or hint)


def a_dir(path, inside, what, hint):
    """A directory named by an environment variable or a path, with the files `inside`."""
    if not path:
        return (False, what, hint)
    missing = [f for f in inside if not os.path.exists(os.path.join(path, f))]
    if missing:
        return (False, what, f"{path} lacks {', '.join(missing)}; {hint}")
    return (True, what, path)


def built(store, names, file, hint):
    """One line per image: built or not (informational)."""
    return [(os.path.exists(os.path.join(store, n, file)), f"image {n}",
             os.path.join(store, n) if os.path.exists(os.path.join(store, n, file)) else hint)
            for n in names]


def checks():
    """{bench: {"need": [(ok, what, detail)], "built": [...]}}."""
    site.load_env()
    zws = os.environ.get("RXRBENCH_ZEPHYR_WORKSPACE")
    nws = os.environ.get("RXRBENCH_NUTTX_WORKSPACE")
    sdk = os.environ.get("ZEPHYR_SDK_INSTALL_DIR")
    out = {}

    py = sys.version_info[:3]
    out["workstation"] = {"need": [
        (py >= (3, 12), "python 3.12 or newer (the venv's)", ".".join(map(str, py))),
        on_path("just", "https://just.systems (the recipes)"),
        on_path("git", "apt install git"),
        on_path("ssh", "apt install openssh-client"),
        on_path("uv", "https://docs.astral.sh/uv (the venvs the default install makes; optional here)"),
    ], "built": []}

    # the simulated Zephyr boards: a workspace with west, CMake 3.28 or newer (the workspace
    # venv's or PATH's), the host build tools; the MCUmgr client venv the SMP module runs with
    cmake_v = None
    cmake_where = ""
    for cand in ([os.path.join(zws, ".venv", "bin", "cmake")] if zws else []) + ["cmake"]:
        cmake_v = version_of([cand, "--version"])
        if cmake_v:
            cmake_where = cand
            break
    venv_west = os.path.join(zws or "", ".venv", "bin", "west")
    west = venv_west if os.access(venv_west, os.X_OK) else shutil.which("west")
    out["zephyr"] = {"need": [
        a_dir(zws, ["zephyr"], "RXRBENCH_ZEPHYR_WORKSPACE (a west workspace: zephyr/ in it)",
              "a west workspace on Zephyr 4.5 (docs/SIMULATED.md)"),
        (west is not None, "west (the workspace venv's, or PATH's)", west or "pip install west"),
        (bool(cmake_v) and cmake_v >= (3, 28), "cmake 3.28 or newer",
         f"{cmake_where} {'.'.join(map(str, cmake_v))}" if cmake_v else "pip install cmake into the workspace venv, or apt install cmake"),
        on_path("ninja", "apt install ninja-build"),
        on_path("gperf", "apt install gperf"),
        on_path("dtc", "apt install device-tree-compiler"),
        on_path("gcc", "apt install gcc g++ (native_sim builds with the host compiler)"),
    ], "built": built(os.path.join(STORE, "zephyr"), ["zephyr-shell", "zephyr-shell-b", "zsmp", "zsmp-b"], "manifest.json", "just sim-zephyr-build")
        + built(os.path.join(STORE, "zephyr"), ["zapp-1.0", "zapp-1.1", "zapp-2.0"], "manifest.json", "just sim-zapp-build")}

    # the SMP flasher runs its module under the MCUmgr client venv, as on a bench host; the
    # sim tests of the SMP server and the emulated MCU need it, the builds do not
    out["smp"] = {"need": [
        (os.access(MCUMGR_PY, os.X_OK), "~/.venv-mcumgr (the MCUmgr client venv)",
         MCUMGR_PY if os.access(MCUMGR_PY, os.X_OK) else "uv venv --python 3.13 --seed ~/.venv-mcumgr && ~/.venv-mcumgr/bin/pip install -r collections/ansible_collections/rxrbench/core/host/venvs/mcumgr.txt"),
    ], "built": []}

    # the SDK's layout: gnu/<target>/bin since 1.0, <target>/bin before
    objcopy = next((c for c in (os.path.join(sdk or "", *sub, "arm-zephyr-eabi", "bin", "arm-zephyr-eabi-objcopy")
                                for sub in (("gnu",), ())) if sdk and os.access(c, os.X_OK)), None)
    out["qemu"] = {"need": [
        (objcopy is not None, "ZEPHYR_SDK_INSTALL_DIR with the Arm toolchain",
         objcopy or "west sdk install -t arm-zephyr-eabi (docs/SIMULATED.md"),
        on_path("qemu-system-arm", "apt install qemu-system-arm"),
    ], "built": built(os.path.join(STORE, "zephyr"), ["zq", "zq-b"], "manifest.json", "just sim-qemu-build")}

    kconfiglib = False
    if nws and os.access(os.path.join(nws, ".venv", "bin", "python"), os.X_OK):
        r = subprocess.run([os.path.join(nws, ".venv", "bin", "python"), "-c", "import kconfiglib"],
                           capture_output=True, check=False)
        kconfiglib = r.returncode == 0
    out["nuttx"] = {"need": [
        a_dir(nws, [os.path.join("nuttx", "Makefile"), "apps"], "RXRBENCH_NUTTX_WORKSPACE (nuttx/ and apps/)",
              "clone apache/nuttx and apache/nuttx-apps side by side (docs/SIMULATED.md"),
        (kconfiglib, "the workspace venv with kconfiglib", "python3 -m venv <workspace>/.venv && <workspace>/.venv/bin/pip install kconfiglib"),
        on_path("make", "apt install make"),
        on_path("gcc", "apt install gcc"),
    ], "built": built(os.path.join(STORE, "nuttx"), ["nsh", "nsh-b"], "nuttx", "just sim-nuttx-build")}

    kvm = vmhost.have_kvm()
    base_img = os.path.join(CACHE, "vmhost", "base.img")
    out["vmhost"] = {"need": [
        on_path("qemu-system-x86_64", "apt install qemu-system-x86"),
        (kvm, "KVM usable (speed; the host runs under TCG without it)",
         "/dev/kvm" if kvm else "add the user to the kvm group (a new login); on WSL2, nested virtualization in .wslconfig"),
    ], "built": [(os.path.exists(base_img), "the cloud image", base_img if os.path.exists(base_img) else "just sim-host-setup")]}

    disk = os.path.join(CACHE, "simvm", "image", "disk.wic")
    sandbox = os.path.join(CACHE, "simvm", "sandbox", "u-boot")
    out["ab"] = {"need": [
        on_path("qemu-system-arm", "apt install qemu-system-arm"),
    ], "built": [
        (os.path.exists(disk), "the A/B image", disk if os.path.exists(disk) else "built with the linux plugin's sim/vm/kas on a build host, then simvm.py setup --image"),
        (os.access(sandbox, os.X_OK), "the U-Boot sandbox binary", sandbox if os.access(sandbox, os.X_OK) else "just sim-vm-sandbox (mainline U-Boot, minutes), then simvm.py setup --sandbox ~/.cache/rxrbench/simvm/sandbox-built"),
    ]}

    docker = shutil.which("docker")
    docker_ok = bool(docker) and subprocess.run([docker, "info"], capture_output=True, timeout=20,
                                                check=False).returncode == 0
    out["containers"] = {"need": [
        (docker_ok, "Docker, answering (`docker info`)",
         docker if docker_ok else ("the daemon is not running, or this user may not use it (the docker group)"
                                   if docker else "install Docker Engine (docs/GETTING-STARTED.md#requirements)")),
        on_path("ssh-keygen", "apt install openssh-client"),
    ], "built": []}

    out["secrets"] = {"need": [
        on_path("sops", "just pins install sops age (pinned; `just setup` does it)"),
        on_path("age", "just pins install sops age (pinned; `just setup` does it)"),
    ], "built": []}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--require", default="", help="benches that must be able to run (exit 1 otherwise)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    result = checks()
    required = {b for b in a.require.split(",") if b}
    unknown = required - set(result)
    if unknown:
        sys.exit(f"doctor: unknown bench {', '.join(sorted(unknown))} (known: {', '.join(result)})")
    if a.json:
        print(json.dumps(result, indent=1))
    else:
        for bench, r in result.items():
            print(f"{bench}{'  (required)' if bench in required else ''}")
            for ok, what, detail in r["need"]:
                print(f"  {'ok  ' if ok else 'need'}  {what}: {detail}")
            for ok, what, detail in r["built"]:
                print(f"  {'built' if ok else 'not built'}  {what}: {detail}")
    lacking = [b for b in required if not all(ok for ok, _, _ in result[b]["need"])]
    if lacking:
        print(f"doctor: cannot run {', '.join(sorted(lacking))} on this machine (see `need` above)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
