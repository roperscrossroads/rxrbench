#!/usr/bin/env python3
"""The pinned single-file tools (core/host/binaries.yml), on this machine.

    pins.py list                      what is pinned, and what this machine has of it
    pins.py install [name ...]        download, check the sha256, unpack, link into bin/
    pins.py path                      the bin/ directory (for PATH)

The same table and the same layout as the default install on a bench host (role
default_install): ~/.local/share/rxrbench/cache/ (downloads), pkg/<name>-<version>/ (unpacked),
bin/ (links). `just setup` installs sops and age on the
workstation with it; CI installs what its jobs need. Nothing outside that directory is
touched, and a download whose checksum differs is refused and removed.
"""
import argparse
import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "..", "collections", "ansible_collections", "rxrbench", "core",
                     "host", "binaries.yml")
ROOT = os.path.expanduser("~/.local/share/rxrbench")
ARCHES = {"aarch64": "aarch64", "arm64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}


def table():
    with open(TABLE) as fh:
        return yaml.safe_load(fh)


def arch():
    m = platform.machine().lower()
    if m not in ARCHES:
        sys.exit(f"pins: no pins for this architecture ({m}); aarch64 and x86_64 are pinned")
    return ARCHES[m]


def fmt(pin):
    return pin.get("format") or ("tar.xz" if pin["url"].endswith(".xz") else "tar.gz")


def url(pin, a):
    return pin["url"].replace("{version}", pin["version"]).replace(
        "{arch}", (pin.get("arch") or {}).get(a, a))


def pkg_dir(name, pin):
    return os.path.join(ROOT, "pkg", f"{name}-{pin['version']}")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(name, pin, a):
    """The release file in cache/, checked; a mismatch is removed and refused."""
    want = (pin.get("sha256") or {}).get(a)
    if not want:
        sys.exit(f"pins: {name} {pin['version']} has no sha256 for {a}")
    ext = "" if fmt(pin) == "raw" else "." + fmt(pin)
    dest = os.path.join(ROOT, "cache", f"{name}-{pin['version']}-{a}{ext}")
    if os.path.exists(dest) and sha256(dest) == want:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest))
    os.close(fd)
    try:
        with urllib.request.urlopen(url(pin, a), timeout=120) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh)
        got = sha256(tmp)
        if got != want:
            raise SystemExit(f"pins: {name} {pin['version']}: sha256 {got} is not the pinned {want}")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest


def unpack(name, pin, src):
    """Into pkg/<name>-<version>/: a bare executable as its file, an archive with its top
    directory stripped (as `tar --strip-components=1`), only the members named in `files`."""
    d = pkg_dir(name, pin)
    os.makedirs(d, exist_ok=True)
    if fmt(pin) == "raw":
        out = os.path.join(d, pin["files"][0])
        shutil.copyfile(src, out)
        os.chmod(out, 0o755)
        return
    want = set(pin["files"])
    with tarfile.open(src) as tf:
        for m in tf.getmembers():
            parts = m.name.split("/", 1)
            base = parts[1] if len(parts) == 2 else parts[0]
            if not m.isfile() or base not in want:
                continue
            out = os.path.join(d, base)
            with tf.extractfile(m) as fh, open(out + ".tmp", "wb") as o:
                shutil.copyfileobj(fh, o)
            os.chmod(out + ".tmp", 0o755)
            os.replace(out + ".tmp", out)
    missing = [f for f in pin["files"] if not os.path.exists(os.path.join(d, f))]
    if missing:
        sys.exit(f"pins: {name} {pin['version']}: the release has no {', '.join(missing)}")


def link(name, pin):
    bindir = os.path.join(ROOT, "bin")
    os.makedirs(bindir, exist_ok=True)
    for f in pin["files"]:
        dst = os.path.join(bindir, f)
        if os.path.lexists(dst):
            os.remove(dst)
        os.symlink(os.path.join(pkg_dir(name, pin), f), dst)


def installed(name, pin):
    return all(os.path.exists(os.path.join(pkg_dir(name, pin), f)) for f in pin["files"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("path")
    i = sub.add_parser("install")
    i.add_argument("names", nargs="*", help="default: every pinned tool")
    a = ap.parse_args()
    pins = table()
    if a.cmd == "path":
        print(os.path.join(ROOT, "bin"))
        return
    if a.cmd == "list":
        for name, pin in pins.items():
            have = "installed" if installed(name, pin) else "-"
            print(f"{name:10s} {pin['version']:10s} {have}")
        return
    unknown = [n for n in a.names if n not in pins]
    if unknown:
        sys.exit(f"pins: not pinned: {', '.join(unknown)} (pinned: {', '.join(pins)})")
    ar = arch()
    for name in a.names or list(pins):
        pin = pins[name]
        if not installed(name, pin):
            unpack(name, pin, fetch(name, pin, ar))
        link(name, pin)
        print(f"{name} {pin['version']}: {os.path.join(ROOT, 'bin')}")


if __name__ == "__main__":
    main()
