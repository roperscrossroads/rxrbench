#!/usr/bin/env python3
"""The simulated Linux bench: each device is a container with an ssh server and nothing else.

    simlinux.py up [name ...]     build the image if it is missing, start the devices
    simlinux.py down              stop and remove them
    simlinux.py reimage <name>    a device comes back new: another host key, its files gone
    simlinux.py pin [name ...]    pin the host keys the devices hold now

A device is `rxrbench-sim-<name>`, with ssh on a loopback port its registry entry names
(sim/registry.yaml). The key that opens the devices and the file their host keys are pinned
in live in ~/.cache/rxrbench/simlinux/, where the registry's routes point. The devices are
reached exactly as a real one is: the tool is given a registry and a layout, and drives ssh.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = "rxrbench-simlinux"
STATE = os.path.expanduser("~/.cache/rxrbench/simlinux")
KEY, KNOWN = os.path.join(STATE, "id_ed25519"), os.path.join(STATE, "known_hosts")
REGISTRY = os.path.join(HERE, "registry.yaml")


def available():
    """Can the simulated bench run here: Docker answers."""
    if not shutil.which("docker") or not shutil.which("ssh-keygen"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def docker(*argv, check=True, timeout=180):
    r = subprocess.run(["docker", *argv], capture_output=True, text=True, timeout=timeout, check=False)
    if check and r.returncode != 0:
        sys.exit(f"docker {' '.join(argv[:2])}: {r.stderr.strip()[-300:]}")
    return r


def devices():
    """{name: its loopback ssh port}, from the registry the layouts name."""
    with open(REGISTRY) as fh:
        reg = yaml.safe_load(fh)
    return {name: int(d["reach"]["ssh"]["port"]) for name, d in reg["devices"].items()}


def container(name):
    return f"rxrbench-sim-{name}"


def ensure_key():
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    os.chmod(STATE, 0o700)
    if not os.path.exists(KEY):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "rxrbench-simlinux",
                        "-f", KEY], check=True)
    with open(KEY + ".pub") as fh:
        return fh.read().strip()


def pin(names):
    """Write the devices' host keys where their routes say they are pinned, read from the
    devices themselves (not from the network: that is what pinning is for)."""
    ports = devices()
    kept = []
    if os.path.exists(KNOWN):
        with open(KNOWN) as fh:
            kept = [ln for ln in fh.read().splitlines()
                    if ln and not any(ln.startswith(f"[127.0.0.1]:{ports[n]} ") for n in names)]
    for name in names:
        end = time.monotonic() + 20
        while True:
            r = docker("exec", container(name), "cat", "/etc/ssh/ssh_host_ed25519_key.pub", check=False)
            if r.returncode == 0 and r.stdout.strip():
                break
            if time.monotonic() > end:
                sys.exit(f"{name}: no host key after 20 s: {r.stderr.strip()[-200:]}")
            time.sleep(0.3)
        kind, blob = r.stdout.split()[:2]
        kept.append(f"[127.0.0.1]:{ports[name]} {kind} {blob}")
    with open(KNOWN, "w") as fh:
        fh.write("\n".join(kept) + "\n")


def start(name, public):
    docker("rm", "-f", container(name), check=False)
    docker("run", "-d", "--name", container(name), "--hostname", name, "--restart", "always",
           "-p", f"127.0.0.1:{devices()[name]}:22",
           "-e", f"SIM_NAME={name}", "-e", f"SIM_AUTHORIZED_KEY={public}", IMAGE)


def answers(name, seconds=30):
    end = time.monotonic() + seconds
    argv = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-i", KEY,
            "-o", "IdentitiesOnly=yes", "-o", f"UserKnownHostsFile={KNOWN}",
            "-o", "StrictHostKeyChecking=yes", "-p", str(devices()[name]), "root@127.0.0.1", "true"]
    while time.monotonic() < end:
        if subprocess.run(argv, capture_output=True, timeout=15, check=False).returncode == 0:
            return True
        time.sleep(0.5)
    return False


def up(names):
    if docker("image", "inspect", IMAGE, check=False).returncode != 0:
        docker("build", "-q", "-t", IMAGE, os.path.join(HERE, "container"), timeout=600)
    public = ensure_key()
    for name in names:
        start(name, public)
    pin(names)
    for name in names:
        if not answers(name):
            sys.exit(f"{name}: no answer over ssh on 127.0.0.1:{devices()[name]}")
        print(f"{name}: container {container(name)}, ssh 127.0.0.1:{devices()[name]}", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["up", "down", "reimage", "pin"])
    ap.add_argument("names", nargs="*")
    a = ap.parse_args(argv)
    known = devices()
    names = a.names or sorted(known)
    for n in names:
        if n not in known:
            sys.exit(f"{n}: not in {REGISTRY}")
    if not available():
        sys.exit("the simulated Linux bench needs Docker and ssh-keygen")
    if a.action == "down":
        for n in names:
            docker("rm", "-f", container(n), check=False)
        print("stopped")
    elif a.action == "pin":
        pin(names)
        print("pinned")
    elif a.action == "reimage":
        if len(a.names) != 1:
            sys.exit("name the one device to re-image")
        up(names)
    else:
        up(names)


if __name__ == "__main__":
    main()
