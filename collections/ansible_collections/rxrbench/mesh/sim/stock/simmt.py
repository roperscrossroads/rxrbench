#!/usr/bin/env python3
"""The simulated stock Meshtastic bench: each node is meshtasticd (stock Meshtastic's own Linux
build) with its simulated radio, in a container, its phone API on a loopback port.

    simmt.py up [name ...]     start the nodes (their config kept from the last run)
    simmt.py down              stop and remove them (their config kept)
    simmt.py wipe <name>       the node comes back new: its config and identity key gone,
                               as after a factory reset
    simmt.py pull              fetch the pinned image

A node is container `rxrbench-sim-<name>`, its file system the volume `rxrbench-sim-<name>`,
its hardware id fixed per name (so its node id is stable), its port the one its registry entry
names (sim/stock/registry.yaml). The image is pinned by digest below (`pull` fetches it).
"""
import argparse
import hashlib
import shutil
import socket
import subprocess
import sys
import time
import os

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.join(HERE, "registry.yaml")
# The 2.7.26 stable release's Debian image, tagged by digest: the tag says which release, the
# digest is what is pulled and run (a tag can be moved; the digest is what was tested).
IMAGE = ("meshtastic/meshtasticd:2.7.26.54e0d8d-beta-debian"
         "@sha256:23e92b1331a3a471eaef0c63cbca4365ca40b3111a9781cfdbe5a5114e5773d4")
FSDIR = "/var/lib/meshtasticd"


def available():
    """Can the simulated nodes run here: Docker answers."""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def docker(*argv, check=True, timeout=300):
    return subprocess.run(["docker", *argv], capture_output=True, text=True, timeout=timeout, check=check)


def nodes():
    with open(REGISTRY) as f:
        devices = yaml.safe_load(f)["devices"]
    return {name: d["reach"]["wifi"]["port"] for name, d in devices.items()}


def hwid(name):
    """A stable, locally administered MAC per node name: the node id follows from it."""
    h = hashlib.sha256(name.encode()).digest()
    return ":".join(f"{b:02x}" for b in (0x02, 0x00, h[0], h[1], h[2], h[3]))


def wait_port(port, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def up(names, erase=False):
    known = nodes()
    for name in names or known:
        if name not in known:
            sys.exit(f"{name}: not in {REGISTRY}")
        port, c = known[name], f"rxrbench-sim-{name}"
        docker("rm", "-f", c, check=False)
        if erase:
            # the file system goes with its volume (meshtasticd's own -e would be repeated by
            # the restart policy on every reboot)
            docker("volume", "rm", "-f", c, check=False)
        # meshtasticd exits on a reboot (a region change, a factory reset); the container comes
        # back as a board does.
        docker("run", "-d", "--name", c, "--restart", "unless-stopped", "-p", f"127.0.0.1:{port}:4403",
               "-v", f"{c}:{FSDIR}", IMAGE,
               "meshtasticd", "-s", "-h", hwid(name), "-d", FSDIR)
        if not wait_port(port):
            sys.exit(f"{name}: the API did not open on 127.0.0.1:{port}\n" + docker("logs", c, check=False).stdout[-2000:])
        print(f"{name}: up on 127.0.0.1:{port}{' (erased)' if erase else ''}")


def down(names=None, erase=False):
    """Stop nodes (all by default). Their config stays in each node's volume for the next `up`,
    unless `erase`: a test leaves nothing behind (no container, which would restart with the
    machine, and no volume)."""
    for name in names or nodes():
        c = f"rxrbench-sim-{name}"
        docker("rm", "-f", c, check=False)
        if erase:
            docker("volume", "rm", "-f", c, check=False)
    print("down" + (" (erased)" if erase else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up").add_argument("names", nargs="*")
    d = sub.add_parser("down")
    d.add_argument("names", nargs="*")
    d.add_argument("--erase", action="store_true", help="remove each node's volume too")
    sub.add_parser("wipe").add_argument("name")
    sub.add_parser("pull")
    a = ap.parse_args()
    if a.cmd == "up":
        up(a.names)
    elif a.cmd == "down":
        down(a.names, erase=a.erase)
    elif a.cmd == "pull":
        docker("pull", "-q", IMAGE, timeout=900)
        print(IMAGE)
    else:
        up([a.name], erase=True)


if __name__ == "__main__":
    main()
