#!/usr/bin/env python3
"""Build a NuttX configuration for its `sim` board and stage it as an image of the PC bench.

    nuttx_sim.py build --name NAME [--config sim:nsh] [--workspace W] [--set CONFIG_X=y ...]
                       [--family nuttx_nsh]

W is a directory holding `nuttx/` and `apps/` (default: RXRBENCH_NUTTX_WORKSPACE), as NuttX's
own instructions lay them out; `--set` changes options after the configuration is applied
(`CONFIG_X=y`, `CONFIG_X=n`, `CONFIG_X="text"`). The result is `.build/store/nuttx/NAME/nuttx`
with a manifest (family, format sim-exe, hash, build), the same shape tools/zephyr_sim.py
stages, so a sim board runs it the same way. The tool brings no NuttX and no toolchain: the
sim board builds with the host compiler; `genromfs` is not needed (the startup ROMFS is off);
`kconfig-tweak`, the one thing the configure wants from kconfig-frontends, is provided when
the host has none (the Kconfig reads are kconfiglib's, from the workspace's venv).
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, *[".."] * 5))
STORE = os.path.join(REPO, ".build", "store", "nuttx")
# What every build here takes (docs/HARDWARE.md, "Simulated hosts and NuttX's sim"):
# - no startup ROMFS (it needs genromfs, for nothing the bench uses);
# - a shell line long enough for a certificate written in one `echo` (NSH's default line of
#   80 characters drops the end of a longer one silently);
# - the console in single-character mode, not the sim's default DMA mode, which holds every
#   256th byte until the next one arrives: a command whose echo or marker lands on that byte
#   is never answered.
# The sim has no reset, only a power-off (`poweroff`: the process exits, and the bench boots it
# again), which is the family's reboot.
SET = ("CONFIG_ETC_ROMFS=n", "CONFIG_NSH_ROMFSETC=n", "CONFIG_LINE_MAX=512", "CONFIG_SIM_UART_DMA=n")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tweak(config, setting):
    """One CONFIG_X=... applied to a .config (what kconfig-tweak would do)."""
    key, _, value = setting.partition("=")
    line = f"# {key} is not set" if value == "n" else f"{key}={value}"
    with open(config) as f:
        lines = f.read().splitlines()
    out, hit = [], False
    for ln in lines:
        if ln.startswith(key + "=") or ln == f"# {key} is not set":
            out.append(line)
            hit = True
        else:
            out.append(ln)
    if not hit:
        out.append(line)
    with open(config, "w") as f:
        f.write("\n".join(out) + "\n")


def said_of_itself(exe):
    """What the image says it is (`uname -a`: release, the build's commit and time, the
    machine), run once on a pty here: the identity a board running it reports, so a bench
    can tell one build from another (two builds of one commit differ by their time)."""
    import pty  # pylint: disable=import-outside-toplevel
    import select  # pylint: disable=import-outside-toplevel
    import tty  # pylint: disable=import-outside-toplevel
    import time  # pylint: disable=import-outside-toplevel
    import re  # pylint: disable=import-outside-toplevel
    m, s = pty.openpty()
    tty.setraw(s)
    proc = subprocess.Popen([exe], stdin=s, stdout=s, stderr=subprocess.DEVNULL, close_fds=True)
    os.close(s)
    out, end = b"", time.monotonic() + 10

    def until(cond):
        nonlocal out
        while time.monotonic() < end and not cond(out):
            r, _, _ = select.select([m], [], [], 0.2)
            if r:
                try:
                    out += os.read(m, 4096)
                except OSError:
                    return
    prompt = re.compile(rb"> (\x1b\[K)?\s*$")      # whatever the prompt string, it ends so
    try:
        until(lambda o: prompt.search(o))           # the shell is listening
        for _ in range(3):
            time.sleep(0.3)                         # input in the instant after the prompt is dropped
            os.write(m, b"uname -a\n")
            mark = len(out)
            until(lambda o, k=mark: b"NuttX " in o[k:] and prompt.search(o[k:]))
            if b"NuttX " in out[mark:]:
                break
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        os.close(m)
    text = out.decode(errors="replace")
    mm = re.search(r"^NuttX (\S+) ([0-9a-f]+) (\w+ +\d+ \d+ [\d:]+) (\S+) (\S+)\s*$", text, re.M)
    if not mm:
        sys.exit(f"{exe}: did not say what it is to `uname -a` ({text[-200:]!r})")
    return {"version": mm.group(1), "build": f"{mm.group(2)} {mm.group(3)}", "target": mm.group(4)}


# NuttX's host setup (tools/sethost.sh, run by configure.sh -l) sets the host kind with
# kconfig-frontends' kconfig-tweak, `--file F --enable|--disable CONFIG_X`, and that is the
# only use the configure makes of that package. A host without it gets this one, on PATH for
# the configure; the build after it reads Kconfig with kconfiglib from the workspace's venv.
KCONFIG_TWEAK = '''#!/usr/bin/env python3
"""kconfig-tweak as NuttX's host setup uses it: --file F --enable CONFIG_X | --disable CONFIG_X."""
import re
import sys
a = sys.argv[1:]
f = a[a.index("--file") + 1]
on = "--enable" in a
key = a[a.index("--enable" if on else "--disable") + 1]
new = f"{key}=y" if on else f"# {key} is not set"
pat = re.compile(rf"^(# )?{re.escape(key)}(=| is not set)")
with open(f) as fh:
    lines = fh.read().splitlines()
out = [new if pat.match(ln) else ln for ln in lines]
if not any(pat.match(ln) for ln in lines):
    out.append(new)
with open(f, "w") as fh:
    fh.write("\\n".join(out) + "\\n")
'''


def kconfig_tweak_dir():
    """A directory holding kconfig-tweak when the host has none (to put first on PATH), else
    None."""
    if shutil.which("kconfig-tweak"):
        return None
    import tempfile  # pylint: disable=import-outside-toplevel
    d = tempfile.mkdtemp(prefix="rxrbench-kconfig-")
    p = os.path.join(d, "kconfig-tweak")
    with open(p, "w") as f:
        f.write(KCONFIG_TWEAK)
    os.chmod(p, 0o755)
    return d


def commit_of(tree):
    r = subprocess.run(["git", "-C", tree, "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def build(a):
    ws = os.path.abspath(os.path.expanduser(a.workspace or os.environ.get("RXRBENCH_NUTTX_WORKSPACE") or ""))
    if not a.workspace and not os.environ.get("RXRBENCH_NUTTX_WORKSPACE"):
        sys.exit("no workspace: --workspace W, or RXRBENCH_NUTTX_WORKSPACE=W (a directory holding nuttx/ and apps/)")
    nuttx = os.path.join(ws, "nuttx")
    if not os.path.isfile(os.path.join(nuttx, "Makefile")) or not os.path.isdir(os.path.join(ws, "apps")):
        sys.exit(f"{ws}: no nuttx/ and apps/ in it")
    env = {**os.environ}
    venv = os.path.join(ws, ".venv", "bin")
    if os.path.isdir(venv):
        env["PATH"] = venv + os.pathsep + env["PATH"]     # kconfiglib's menuconfig, which NuttX prefers
    shim = kconfig_tweak_dir()
    if shim:
        env["PATH"] = shim + os.pathsep + env["PATH"]     # the host has no kconfig-frontends
    def run(cmd, log):
        path = os.path.join(ws, log)
        with open(path, "w") as f:
            r = subprocess.run(cmd, cwd=nuttx, env=env, stdout=f, stderr=subprocess.STDOUT, check=False)
        if r.returncode != 0:
            # the log's tail, so a run whose workspace does not outlive it (CI) still says why
            with open(path, errors="replace") as f:
                tail = f.read().splitlines()[-25:]
            sys.exit(f"{' '.join(cmd)} failed (rc {r.returncode}): see {path}; its last lines:\n"
                     + "\n".join("  " + ln for ln in tail))
    print(f"+ configure {a.config} in {nuttx}", flush=True)
    run(["make", "distclean"], "distclean.log")
    run(["./tools/configure.sh", "-l", a.config], "configure.log")
    for setting in SET + tuple(a.set):
        tweak(os.path.join(nuttx, ".config"), setting)
    run(["make", "olddefconfig"], "olddefconfig.log")
    print("+ make", flush=True)
    run(["make", f"-j{os.cpu_count() or 2}"], "build.log")
    exe = os.path.join(nuttx, "nuttx")
    if not os.access(exe, os.X_OK):
        sys.exit(f"{exe}: the build made no executable")
    out = os.path.join(STORE, a.name)
    os.makedirs(out, exist_ok=True)
    shutil.copy2(exe, os.path.join(out, "nuttx.new"))
    os.replace(os.path.join(out, "nuttx.new"), os.path.join(out, "nuttx"))
    manifest = {"family": a.family, "format": "sim-exe", "file": "nuttx",
                "board_tag": "nuttx-sim", "config": a.config, "set": list(a.set),
                "hash": sha256_of(exe), **said_of_itself(exe)}
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f)
        f.write("\n")
    print(f"staged {out} (hash {manifest['hash'][:12]}, nuttx {manifest['build']})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="configure, build and stage")
    b.add_argument("--name", required=True, help="the image's name in the store")
    b.add_argument("--config", default="sim:nsh", help="a NuttX board:config (default sim:nsh)")
    b.add_argument("--workspace", help="nuttx/ and apps/ (default RXRBENCH_NUTTX_WORKSPACE)")
    b.add_argument("--set", action="append", default=[], help="CONFIG_X=y|n|\"text\", after the configuration")
    b.add_argument("--family", default="nuttx_nsh", help="the image's family (default nuttx_nsh)")
    a = ap.parse_args(argv)
    build(a)


if __name__ == "__main__":
    main()
