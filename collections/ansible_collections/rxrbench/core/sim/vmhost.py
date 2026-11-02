#!/usr/bin/env python3
"""A simulated bench host: a stock cloud image in QEMU, reached over ssh like a Pi.

    vmhost.py setup                     fetch the pinned cloud image, make the keys (once)
    vmhost.py up [--name N] [--port P]  boot it and serve until SIGTERM (KVM when there is one)
    vmhost.py down [--name N]           stop it
    vmhost.py fresh [--name N]          a new disk from the base image: the host as delivered
    vmhost.py ssh [--name N] [CMD...]   a shell, or a command, in it
    vmhost.py registry [--name N]       the registry `hosts:` entry that reaches it
    vmhost.py missing                   what this machine lacks to run one (nothing: can run)

The image is Ubuntu's server cloud image, pinned by release and checked against its published
hashes; nothing is built. cloud-init gives it a user `bench` with the key made here, python
and ssh it has already. The host sees the guest on a loopback port; the guest sees the host
as 10.0.2.2 (QEMU's own user-network gateway; privacy: allow), where the seed is served from. What this
makes lives under ~/.cache/rxrbench/vmhost/ (RXRBENCH_VMHOST_DIR):

    base.img                 the cloud image
    id_ed25519, .pub         the key that opens every instance
    known_hosts              the instances' host keys, pinned on first boot
    run/<name>/              one instance: disk.qcow2, seed/, console.log, pid, port
"""
import argparse
import fcntl
import hashlib
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

RELEASE = "https://cloud-images.ubuntu.com/releases/noble/release-20260926"
IMAGE = "ubuntu-24.04-server-cloudimg-amd64.img"
# The image's hash, pinned here: the release's SHA256SUMS comes from the same server as the
# image, so it is a cross-check, not the pin. A new release records its hash here.
IMAGE_SHA256 = "6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2"
STATE = os.path.expanduser(os.environ.get("RXRBENCH_VMHOST_DIR", "~/.cache/rxrbench/vmhost"))
BASE, KEY, KNOWN = (os.path.join(STATE, n) for n in ("base.img", "id_ed25519", "known_hosts"))
RUN = os.path.join(STATE, "run")
USER = "bench"
GATEWAY = "10.0.2.2"      # QEMU's user network gateway, every guest's view of this machine (privacy: allow)
RAM_MB, CPUS = 1536, 2


class VmError(Exception):
    pass


def missing():
    """What this machine lacks to run a simulated bench host, as a list of words."""
    out = [t for t in ("qemu-system-x86_64", "qemu-img", "ssh-keygen", "ssh") if not shutil.which(t)]
    return out


KVM_GET_API_VERSION = 0xAE00
KVM_API_VERSION = 12


def have_kvm():
    """Whether KVM is usable, not merely present: the node is opened and asked its API version.
    A `/dev/kvm` the user may open is not enough: on a WSL2 kernel without nested
    virtualization the node exists and every open fails with ENODEV, and QEMU asked for `kvm`
    then exits before the machine boots. That answer is the one to trust, never the node's mode."""
    try:
        fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        return fcntl.ioctl(fd, KVM_GET_API_VERSION) == KVM_API_VERSION
    except OSError:
        return False
    finally:
        os.close(fd)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def published_hash():
    """The image's hash from the release's SHA256SUMS (over https, the same place the image
    comes from)."""
    with urllib.request.urlopen(f"{RELEASE}/SHA256SUMS", timeout=60) as r:
        for line in r.read().decode().splitlines():
            digest, _, name = line.partition(" ")
            if name.strip().lstrip("*") == IMAGE:
                return digest.strip()
    raise VmError(f"{RELEASE}/SHA256SUMS does not name {IMAGE}")


def setup(a):
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    want = a.sha256 or IMAGE_SHA256
    if not a.sha256:
        try:
            published = published_hash()
        except (OSError, VmError):
            published = None      # offline: the pin alone decides
        if published and published != IMAGE_SHA256:
            raise VmError(f"{RELEASE}/SHA256SUMS says {published} for {IMAGE}; this tool pins "
                          f"{IMAGE_SHA256}: refused (a re-published release, or another file)")
    if os.path.isfile(BASE) and sha256_of(BASE) == want:
        print(f"{BASE}: present and checked")
    else:
        tmp = BASE + ".part"
        print(f"fetching {RELEASE}/{IMAGE} ...", flush=True)
        with urllib.request.urlopen(f"{RELEASE}/{IMAGE}", timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        got = sha256_of(tmp)
        if got != want:
            os.unlink(tmp)
            raise VmError(f"the image's hash is {got}, the release says {want}: not kept")
        os.replace(tmp, BASE)
        print(f"{BASE}: fetched and checked ({want[:12]})")
    if not os.path.isfile(KEY):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "rxrbench vmhost",
                        "-f", KEY], check=True)
        print(f"{KEY}: made")
    open(KNOWN, "a").close()


def _run_dir(name):
    return os.path.join(RUN, name)


def _seed(name, run):
    """cloud-init's NoCloud seed for this instance: a user with this tool's key, and periodic
    apt off. A cloud image's apt-daily timers fire soon after a first boot and
    unattended-upgrades then holds the dpkg lock, longer under TCG than an apt task waits for it
    (docs/HARDWARE.md, "Simulated hosts and NuttX's sim"). A simulated bench host is a
    throwaway: it gets no periodic apt."""
    with open(KEY + ".pub") as f:
        pub = f.read().strip()
    seed = os.path.join(run, "seed")
    os.makedirs(seed, exist_ok=True)
    with open(os.path.join(seed, "meta-data"), "w") as f:
        f.write(f"instance-id: rxrbench-{name}-{int(time.time())}\nlocal-hostname: {name}\n")
    with open(os.path.join(seed, "user-data"), "w") as f:
        f.write("#cloud-config\n" + json.dumps({
            "hostname": name,
            "users": [{"name": USER, "shell": "/bin/bash", "lock_passwd": True,
                       "sudo": "ALL=(ALL) NOPASSWD:ALL", "ssh_authorized_keys": [pub]}],
            "ssh_pwauth": False,
            "package_update": False,
            "package_upgrade": False,
            "apt": {"conf": 'APT::Periodic::Enable "0";\nAPT::Periodic::Update-Package-Lists "0";\n'
                            'APT::Periodic::Unattended-Upgrade "0";\n'},
            "runcmd": [["systemctl", "disable", "--now", "apt-daily.timer", "apt-daily-upgrade.timer"]],
        }) + "\n")
    with open(os.path.join(seed, "vendor-data"), "w") as f:
        f.write("")
    return seed


def _serve(directory):
    """The seed over http on a loopback port the guest reaches as QEMU's gateway."""
    # the directory goes to the handler's constructor: a class attribute of that name is
    # overridden there (by the working directory, which is the repository)
    class Seed(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def log_message(self, *_):
            pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Seed)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _free_port(wanted):
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", wanted))
            return wanted
        except OSError as e:
            raise VmError(f"port {wanted} is taken ({e}); --port another") from e


def _ssh_argv(port, batch=True):
    return ["ssh", "-p", str(port), "-i", KEY, "-o", f"UserKnownHostsFile={KNOWN}",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
            *(["-o", "BatchMode=yes"] if batch else []), f"{USER}@127.0.0.1"]


def _still(name, proc, done):
    """The machine is still coming up: its process lives and no stop was asked for."""
    if done.is_set():
        raise VmError(f"{name}: stopped while coming up")
    if proc.poll() is not None:
        raise VmError(f"{name}: qemu exited (rc {proc.returncode}) while coming up "
                      f"(see {_run_dir(name)}/qemu.out and console.log)")


def _pin(name, port, timeout, proc, done):
    """The instance's host key into known_hosts, from its first boot: this tool made the
    machine, so its first key is trusted."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _still(name, proc, done)
        r = subprocess.run(["ssh-keyscan", "-p", str(port), "-T", "5", "127.0.0.1"],
                           capture_output=True, text=True, check=False)
        keys = [ln for ln in r.stdout.splitlines() if ln and not ln.startswith("#")]
        if keys:
            kept = []
            if os.path.isfile(KNOWN):
                with open(KNOWN) as f:
                    kept = [ln for ln in f.read().splitlines() if not ln.startswith(f"[127.0.0.1]:{port} ")]
            with open(KNOWN, "w") as f:
                f.write("\n".join(kept + keys) + "\n")
            return
        done.wait(2)
    raise VmError(f"{name}: no ssh on port {port} after {timeout} s (console: {_run_dir(name)}/console.log)")


def _wait_ssh(name, port, timeout, proc, done):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _still(name, proc, done)
        r = subprocess.run(_ssh_argv(port) + ["cloud-init", "status", "--wait"],
                           capture_output=True, text=True, timeout=120, check=False)
        if r.returncode == 0 or "done" in r.stdout:
            return
        done.wait(3)
    raise VmError(f"{name}: ssh on port {port} does not answer after {timeout} s")


def up(a):
    if missing():
        raise VmError("cannot run a simulated bench host: no " + ", ".join(missing()))
    if not os.path.isfile(BASE) or not os.path.isfile(KEY):
        raise VmError("not set up: vmhost.py setup")
    run = _run_dir(a.name)
    os.makedirs(run, mode=0o700, exist_ok=True)
    if _pid(a.name):
        raise VmError(f"{a.name}: already running (pid {_pid(a.name)})")
    if a.daemon:
        # The same, in a process of its own, and this one returns once the host answers.
        for n in ("up",):
            try:
                os.unlink(os.path.join(run, n))
            except OSError:
                pass
        argv = [sys.executable, os.path.abspath(__file__), "up", "--name", a.name, "--port", str(a.port),
                "--disk-gb", str(a.disk_gb), "--boot-timeout", str(a.boot_timeout)]
        with open(os.path.join(run, "up.out"), "ab") as out:
            child = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        end = time.monotonic() + a.boot_timeout + 30
        while time.monotonic() < end:
            if os.path.isfile(os.path.join(run, "up")):
                print(f"{a.name}: up ({USER}@127.0.0.1:{a.port}, key {KEY}); vmhost.py down stops it")
                return
            if child.poll() is not None:
                with open(os.path.join(run, "up.out")) as f:
                    raise VmError(f"{a.name}: did not come up:\n" + f.read()[-2000:])
            time.sleep(1)
        child.terminate()
        raise VmError(f"{a.name}: not up after {a.boot_timeout} s")
    disk = os.path.join(run, "disk.qcow2")
    if not os.path.isfile(disk):
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", BASE, "-F", "qcow2", disk,
                        f"{a.disk_gb}G"], check=True)
    port = _free_port(a.port)
    seed = _seed(a.name, run)
    httpd, seed_port = _serve(seed)
    accel = "kvm" if have_kvm() else "tcg"
    argv = ["qemu-system-x86_64", "-name", a.name, "-machine", f"q35,accel={accel}",
            "-cpu", "host" if accel == "kvm" else "max", "-smp", str(CPUS), "-m", str(RAM_MB),
            "-display", "none", "-monitor", "none", "-serial", f"file:{os.path.join(run, 'console.log')}",
            "-drive", f"file={disk},if=virtio,format=qcow2",
            "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{port}-:22",
            "-device", "virtio-net-pci,netdev=n0",
            # QEMU's user network: the guest sees this machine as its gateway (privacy: allow)
            "-smbios", f"type=1,serial=ds=nocloud;s=http://{GATEWAY}:{seed_port}/"]
    with open(os.path.join(run, "qemu.out"), "ab") as out:
        proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    # this process owns the machine (`owner`): a signal to it, from `down` or a hand, powers
    # the guest off and cleans up, whether the machine is still coming up or is up
    with open(os.path.join(run, "pid"), "w") as f:
        f.write(str(proc.pid))
    with open(os.path.join(run, "owner"), "w") as f:
        f.write(str(os.getpid()))
    with open(os.path.join(run, "port"), "w") as f:
        f.write(str(port))
    done = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: done.set())
    print(f"{a.name}: qemu pid {proc.pid} ({accel}), ssh port {port}; waiting for it ...", flush=True)
    try:
        _pin(a.name, port, a.boot_timeout, proc, done)
        _wait_ssh(a.name, port, a.boot_timeout, proc, done)
    except VmError:
        _stop(proc)
        _forget(run)
        raise
    print(f"{a.name}: up ({USER}@127.0.0.1:{port}, key {KEY})", flush=True)
    with open(os.path.join(run, "up"), "w") as f:
        f.write(str(port))
    while not done.is_set() and proc.poll() is None:
        done.wait(1)
    httpd.shutdown()
    if proc.poll() is None:
        subprocess.run(_ssh_argv(port) + ["sudo", "poweroff"], capture_output=True, timeout=30, check=False)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _stop(proc)
    _forget(run)
    print(f"{a.name}: down (rc {proc.returncode})")


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    proc.wait()


def _forget(run):
    for n in ("pid", "owner", "up"):
        try:
            os.unlink(os.path.join(run, n))
        except OSError:
            pass


def _alive(path):
    """The pid in that file, when it names a live process (a zombie is not one)."""
    try:
        with open(path) as f:
            pid = int(f.read())
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rpartition(")")[2].split()[0] == "Z":
                return None
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError, IndexError):
        return None


def _pid(name):
    return _alive(os.path.join(_run_dir(name), "pid"))


def _owner(name):
    return _alive(os.path.join(_run_dir(name), "owner"))


def _port(name):
    try:
        with open(os.path.join(_run_dir(name), "port")) as f:
            return int(f.read())
    except (OSError, ValueError) as e:
        raise VmError(f"{name}: not up (no port recorded)") from e


def down(a):
    pid, owner = _pid(a.name), _owner(a.name)
    if pid is None and owner is None:
        _forget(_run_dir(a.name))
        print(f"{a.name}: not running")
        return
    # the `up` process owns the machine: told to stop, it powers the guest off (when the
    # guest is up; a guest still coming up is terminated) and cleans up
    if owner:
        os.kill(owner, signal.SIGTERM)
    end = time.monotonic() + 60
    while (_pid(a.name) or _owner(a.name)) and time.monotonic() < end:
        time.sleep(1)
    if _pid(a.name):
        # no owner to do it (killed by hand): the machine itself
        os.kill(pid, signal.SIGTERM)
        _forget(_run_dir(a.name))
    print(f"{a.name}: down")


def fresh(a):
    if _pid(a.name) or _owner(a.name):
        raise VmError(f"{a.name}: running; down first")
    run = _run_dir(a.name)
    for n in ("disk.qcow2", "console.log", "qemu.out", "port"):
        try:
            os.unlink(os.path.join(run, n))
        except OSError:
            pass
    print(f"{a.name}: fresh (a new disk at the next up)")


def ssh(a):
    port = _port(a.name)
    os.execvp("ssh", _ssh_argv(port, batch=bool(a.command)) + (["--"] + a.command if a.command else []))


def registry(a):
    port = _port(a.name) if _pid(a.name) else a.port
    print(json.dumps({a.name: {"ssh": f"{USER}@127.0.0.1", "port": port, "ssh_key": KEY,
                               "known_hosts": KNOWN, "bench_dir": f"/home/{USER}/rxrbench"}}))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--sha256", help="the image's hash, instead of the one pinned here (another release)")
    for name in ("up", "down", "fresh", "ssh", "registry"):
        p = sub.add_parser(name)
        p.add_argument("--name", default="vmhost1")
        if name in ("up", "registry"):
            p.add_argument("--port", type=int, default=2222, help="the loopback port ssh is forwarded to")
        if name == "up":
            p.add_argument("--disk-gb", type=int, default=8)
            p.add_argument("--boot-timeout", type=int, default=300)
            p.add_argument("--daemon", action="store_true",
                           help="return once the host answers; it runs on until `down`")
        if name == "ssh":
            p.add_argument("command", nargs="*", help="a command to run there; none for a shell")
    sub.add_parser("missing")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "missing":
            print(" ".join(missing()) or "nothing: a simulated bench host can run here"
                  + (" (KVM)" if have_kvm() else " (no KVM: slower)"))
        else:
            {"setup": setup, "up": up, "down": down, "fresh": fresh, "ssh": ssh, "registry": registry}[a.cmd](a)
    except VmError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
