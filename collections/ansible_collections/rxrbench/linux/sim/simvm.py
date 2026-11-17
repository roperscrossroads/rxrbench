#!/usr/bin/env python3
"""The simulated A/B Linux bench: a QEMU machine with RAUC slots, slotguard's health gating
and an ssh server, kept running across its own reboots.

    simvm.py setup --sandbox DIR --image DIR   put the pieces in place (once per image)
    simvm.py up [name ...]                     run the devices until stopped (SIGTERM)
    simvm.py down                              stop them
    simvm.py fresh [name ...]                  factory state again: disk, environment, memory
    simvm.py pin [name ...]                    pin the host keys the devices present now
    simvm.py bundle [--version V] [--foreign]  sign an update bundle from the built image
                    [--broken noinit|unhealthy]  ... one that is validly signed and does not work

A machine has no bootloader inside it. What chooses the slot is the real boot script, run
between boots by a U-Boot sandbox binary on this host against the environment the guest's
own tools write (the design, the script and the environment tool are meta-slotguard's:
its bench/). So every boot goes: run the boot script, start QEMU on the slot it chose, and
when the guest reboots (QEMU exits), do it again. This file is that loop, with an ssh port
forwarded so the tool reaches the device as it does a real one.

Everything it needs and makes is under ~/.cache/rxrbench/simvm/:

    harness/     a checkout of meta-slotguard (setup clones the pinned commit)
    sandbox/     u-boot, u-boot.dtb: the sandbox binary (meta-slotguard bench/build-sandbox.sh)
    image/       disk.wic, zImage: the built image (vm/kas, on a build host)
    run/<name>/  one device: its disk, environment, memory, console log
    id_ed25519, known_hosts, ca.*   the keys (vm/make-keys.sh)
"""
import argparse
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.expanduser("~/.cache/rxrbench/simvm")
HARNESS, SANDBOX, IMAGE = (os.path.join(STATE, d) for d in ("harness", "sandbox", "image"))
RUN = os.path.join(STATE, "run")
KEY, KNOWN = os.path.join(STATE, "id_ed25519"), os.path.join(STATE, "known_hosts")
# The devices are a registry's entries with an ssh route on a loopback port: this plugin's
# own, or the registry `--registry` names (another plugin's simulated device on the same
# machine image, run beside this one's).
REGISTRY = os.path.join(HERE, "vm", "registry.yaml")
HARNESS_URL = "https://github.com/roperscrossroads/meta-slotguard"
HARNESS_REV = "9521b44725d4f039b79f1edef67cd25b88a2f120"   # the commit vm/kas pins
# The boot script's variables that persist between boots (meta-slotguard bench/vm-loop.sh).
TRACKED = ("BOOT_ORDER", "BOOT_A_LEFT", "BOOT_B_LEFT", "BOOT_REQUEST", "BOOT_DELAY_RESTORE")
ENV_BYTES = 0x8000          # the guest's fw_env.config: the whole second disk
RAM_MB = 256


class SimError(Exception):
    pass


def missing():
    """What the simulated bench still lacks here, as a list of words; empty when it can run."""
    out = [] if shutil.which("qemu-system-arm") else ["qemu-system-arm"]
    for path, what in ((os.path.join(HARNESS, "bench", "uboot-env.py"), "the harness (setup)"),
                       (os.path.join(SANDBOX, "u-boot"), "the sandbox binary (setup --sandbox)"),
                       (os.path.join(IMAGE, "disk.wic"), "the image (setup --image)"),
                       (os.path.join(IMAGE, "zImage"), "the kernel (setup --image)"),
                       (os.path.join(IMAGE, "rootfs.ext4"), "the root filesystem (setup --image)"),
                       (KEY, "the ssh key (vm/make-keys.sh)")):
        if not os.path.exists(path):
            out.append(what)
    return out


def available():
    return not missing()


def devices():
    """{name: its loopback ssh port}, from the registry the layouts name."""
    with open(REGISTRY) as fh:
        reg = yaml.safe_load(fh)
    return {name: int(d["reach"]["ssh"]["port"]) for name, d in reg["devices"].items()}


def envtool(*argv):
    r = subprocess.run([sys.executable, os.path.join(HARNESS, "bench", "uboot-env.py"), *argv],
                       capture_output=True, text=True, check=False)
    return r.returncode, r.stdout.strip()


def boot_script():
    """The composed boot script, built once from the harness's own parts."""
    out = os.path.join(STATE, "boot.cmd")
    files = os.path.join(HARNESS, "recipes-bsp", "slotguard-boot", "files")
    r = subprocess.run([sys.executable, os.path.join(files, "slotguard-compose.py"),
                        "--core", os.path.join(files, "slotguard-ab.cmd.in"),
                        "--launch", os.path.join(files, "slotguard-launch.cmd.reference"),
                        "--out", out], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise SimError(f"composing the boot script failed: {r.stderr.strip()[-300:]}")
    with open(out) as fh:
        return fh.read()


# What a terminal would eat: a newer U-Boot colours its output, and the escape bytes would
# otherwise ride along in a value read back from it (a root device the kernel never finds).
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def printed_env(out):
    """The variables `printenv` printed after the ===ENV=== mark, as {name: value}, free of
    carriage returns and terminal escape sequences whatever the sandbox's U-Boot adds."""
    text = ANSI.sub("", out.replace("\r", ""))
    return dict(m.groups() for m in re.finditer(r"^([A-Za-z_]+)=(.*)$", text.partition("===ENV===")[2], re.M))


def oracle(work, script):
    """Run the boot script against the device's environment: (slot, root partition). The
    environment it leaves (attempt counters, order) is written back for the guest to see."""
    envstore = os.path.join(work, "envstore.img")
    prelude = []
    for v in TRACKED:
        rc, val = envtool("get", envstore, v)
        if rc == 0:
            prelude.append(f'setenv {v} "{val}"')
    cmd = "\n".join(prelude) + "\n" + script + "\necho ===ENV===\nprintenv " + " ".join(TRACKED) \
        + " raucslot rootpart"
    r = subprocess.run([os.path.join(SANDBOX, "u-boot"), "-d", os.path.join(SANDBOX, "u-boot.dtb"),
                        "-c", cmd], capture_output=True, text=True, timeout=60, check=False,
                       stdin=subprocess.DEVNULL)
    out = r.stdout + r.stderr
    with open(os.path.join(work, "oracle.log"), "w") as fh:
        fh.write(out.replace("\r", ""))
    env = printed_env(out)
    if not env.get("raucslot") or not env.get("rootpart"):
        raise SimError(f"the boot script chose no slot (see {work}/oracle.log)")
    for v in TRACKED:
        envtool("set", envstore, f"{v}={env.get(v, '')}")
    return env["raucslot"], env["rootpart"]


def fresh(name):
    """A device as it leaves the factory: the built disk, an empty environment, no memory."""
    work = os.path.join(RUN, name)
    os.makedirs(work, exist_ok=True)
    subprocess.run(["cp", "--sparse=always", "-f", os.path.join(IMAGE, "disk.wic"),
                    os.path.join(work, "disk.img")], check=True)
    for f, size in (("envstore.img", ENV_BYTES), ("ram.bin", RAM_MB << 20)):
        with open(os.path.join(work, f), "wb") as fh:
            fh.truncate(size)
    return work


def qemu_argv(work, port, slot, rootpart, log):
    ramoops = "ramoops.mem_address=0x4FF00000 ramoops.mem_size=0x20000 " \
              "ramoops.console_size=0x18000 ramoops.record_size=0x8000"
    return ["qemu-system-arm",
            "-object", f"memory-backend-file,id=mem,size={RAM_MB}M,mem-path={work}/ram.bin,share=on",
            "-machine", "virt,highmem=off,memory-backend=mem", "-cpu", "cortex-a15", "-smp", "2",
            "-m", str(RAM_MB), "-display", "none", "-no-reboot", "-serial", f"file:{log}",
            "-kernel", os.path.join(IMAGE, "zImage"),
            "-drive", f"id=disk0,file={work}/disk.img,if=none,format=raw",
            "-device", "virtio-blk-pci,drive=disk0,addr=0x2",
            "-drive", f"id=env0,file={work}/envstore.img,if=none,format=raw",
            "-device", "virtio-blk-pci,drive=env0,addr=0x3",
            "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{port}-:22",
            "-device", "virtio-net-pci,netdev=net0,addr=0x5",
            "-append", f"console=ttyAMA0,115200 root=/dev/vda{rootpart} rootfstype=ext4 rootwait ro "
                       f"panic=10 rauc.slot={slot} mem=255M {ramoops} slotguard.action=none"]


class Device(threading.Thread):
    """One machine, kept running: boot script, QEMU, and again when the guest reboots."""

    def __init__(self, name, port, script):
        super().__init__(daemon=True)
        self.name_, self.port, self.script = name, port, script
        self.work = os.path.join(RUN, name)
        self.proc, self.stop, self.boots = None, threading.Event(), 0

    def run(self):
        if not os.path.exists(os.path.join(self.work, "disk.img")):
            fresh(self.name_)
        while not self.stop.is_set():
            try:
                slot, rootpart = oracle(self.work, self.script)
            except (SimError, subprocess.TimeoutExpired) as e:
                print(f"{self.name_}: {e}", flush=True)
                return
            self.boots += 1
            log = os.path.join(self.work, f"console-{self.boots:03d}.log")
            print(f"{self.name_}: boot {self.boots} on slot {slot} (root vda{rootpart}), "
                  f"ssh 127.0.0.1:{self.port}", flush=True)
            self.proc = subprocess.Popen(qemu_argv(self.work, self.port, slot, rootpart, log),
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=open(log + ".qemu", "w"))  # pylint: disable=consider-using-with
            self.proc.wait()
            time.sleep(0.5)

    def end(self):
        self.stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def pin(names):
    """Pin the devices' host keys where their routes say: asked of the loopback port each is
    forwarded on. (There is no way into a machine but its network; the port is this host's.)"""
    ports = devices()
    scanned = []
    for name in names:
        end = time.monotonic() + 240
        while True:
            r = subprocess.run(["ssh-keyscan", "-p", str(ports[name]), "127.0.0.1"],
                               capture_output=True, text=True, timeout=30, check=False)
            lines = [ln for ln in r.stdout.splitlines() if ln and not ln.startswith("#")]
            if lines:
                break
            if time.monotonic() > end:
                sys.exit(f"{name}: no ssh server on 127.0.0.1:{ports[name]} after 240 s")
            time.sleep(2)
        scanned += lines
    # The file is every simulated device's, and two may be pinned at once (two tests, two
    # devices): it is read and written back under a lock, so neither pin undoes the other's.
    os.makedirs(os.path.dirname(KNOWN), exist_ok=True)
    with open(KNOWN + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        kept = []
        if os.path.exists(KNOWN):
            with open(KNOWN) as fh:
                kept = [ln for ln in fh.read().splitlines()
                        if ln and not any(ln.startswith(f"[127.0.0.1]:{ports[n]} ") for n in names)]
        tmp = f"{KNOWN}.{os.getpid()}"
        with open(tmp, "w") as fh:
            fh.write("\n".join(kept + scanned) + "\n")
        os.replace(tmp, KNOWN)


# What makes a validly signed bundle not work, as paths removed from its root filesystem:
#   noinit     no init and no shell: the kernel panics before userspace, every boot
#   unhealthy  it boots and answers, but a unit its health gate requires is gone, so the
#              gate fails and the slot is never marked good
BROKEN = {"noinit": ("/usr/sbin/init", "/usr/bin/sh"),
          "unhealthy": ("/usr/lib/systemd/system/storage-guard.service",)}


def bundle(version, foreign=False, out=None, compatible=None, broken=None):
    """Sign an update bundle from the built root filesystem, for the device's own `compatible`
    (read out of the image, never retyped). `foreign` signs it with a key the device does not
    trust, and `compatible` makes it a bundle for some other device, for the tests that each
    is refused. `broken` (BROKEN) makes one the device accepts and that then does not work,
    for the tests that such an update is rolled back. Returns the bundle's path."""
    rootfs = os.path.join(IMAGE, "rootfs.ext4")
    for tool in ("rauc", "mksquashfs", "openssl"):
        if not shutil.which(tool):
            raise SimError(f"{tool} is not installed")
    debugfs = shutil.which("debugfs") or "/sbin/debugfs"
    r = subprocess.run([debugfs, "-R", "cat /usr/lib/rauc/system.conf", rootfs],
                       capture_output=True, text=True, check=False)
    m = re.search(r"^compatible=(\S+)$", r.stdout, re.M)
    if not m:
        raise SimError(f"no compatible= in {rootfs}'s RAUC configuration")
    bundles = os.path.join(STATE, "bundles")
    os.makedirs(bundles, exist_ok=True)
    if broken and broken not in BROKEN:
        raise SimError(f"--broken is one of {', '.join(BROKEN)}")
    kind = "foreign" if foreign else "other" if compatible else f"broken-{broken}" if broken else "update"
    out = out or os.path.join(bundles, f"{kind}-{version}.raucb")
    cert, key = os.path.join(STATE, "ca.cert.pem"), os.path.join(STATE, "ca.key.pem")
    # Two bundles may be made at once (two tests, two devices): each has a work directory of
    # its own, and a bundle appears under its name whole or not at all.
    work = tempfile.mkdtemp(prefix=".work-", dir=bundles)
    os.makedirs(os.path.join(work, "content"))
    made = os.path.join(work, "bundle.raucb")
    try:
        if foreign:
            cert, key = os.path.join(work, "foreign.cert.pem"), os.path.join(work, "foreign.key.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                            "-keyout", key, "-out", cert, "-subj", "/CN=not the device's key"],
                           capture_output=True, check=True)
        subprocess.run(["cp", "--sparse=always", "-L", rootfs, os.path.join(work, "content", "rootfs.img")],
                       check=True)
        for path in BROKEN.get(broken, ()):
            r = subprocess.run([debugfs, "-w", "-R", f"unlink {path}",
                                os.path.join(work, "content", "rootfs.img")],
                               capture_output=True, text=True, check=False)
            if r.returncode != 0 or "not found" in r.stderr:
                raise SimError(f"could not remove {path} from the image: {r.stderr.strip()[-200:]}")
        with open(os.path.join(work, "content", "manifest.raucm"), "w") as fh:
            fh.write(f"[update]\ncompatible={compatible or m.group(1)}\nversion={version}\n\n[bundle]\nformat=verity\n\n"
                     "[image.rootfs]\nfilename=rootfs.img\n")
        r = subprocess.run(["rauc", "bundle", "--mksquashfs-args=-comp zstd", f"--cert={cert}",
                            f"--key={key}", os.path.join(work, "content"), made],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise SimError(f"rauc bundle: {(r.stdout + r.stderr).strip()[-300:]}")
        os.replace(made, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


ARTIFACTS = os.path.join(HERE, "vm", "ARTIFACTS.yml")


def recorded_hashes():
    """{"image": {name: sha256}, "sandbox": {...}}: the builds this tree is known to run with."""
    import yaml  # pylint: disable=import-outside-toplevel
    with open(ARTIFACTS) as f:
        return yaml.safe_load(f) or {}


def version_in(path):
    """The `U-Boot 20xx.yy` string a binary carries, or None."""
    with open(path, "rb") as f:
        m = re.search(rb"U-Boot (20\d\d\.\d\d(?:-rc\d)?)", f.read())
    return m.group(1).decode() if m else None


def sha256_of(path):
    import hashlib  # pylint: disable=import-outside-toplevel
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def setup(sandbox, image, unverified=False):
    """Put the pieces in place. Each is checked against vm/ARTIFACTS.yml, the builds this tree
    is known to run with; another build is refused unless `unverified` (an experiment, or a
    rebuild whose hashes are about to be recorded)."""
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    if not os.path.exists(os.path.join(HARNESS, ".git")):
        subprocess.run(["git", "clone", "--quiet", HARNESS_URL, HARNESS], check=True)
    subprocess.run(["git", "-C", HARNESS, "checkout", "--quiet", HARNESS_REV], check=True)
    known = recorded_hashes()
    for src, dest, key, names in ((sandbox, SANDBOX, "sandbox", ("u-boot", "u-boot.dtb")),
                                  (image, IMAGE, "image", ("disk.wic", "zImage", "rootfs.ext4"))):
        if not src:
            continue
        for n in names:
            p = os.path.join(os.path.expanduser(src), n)
            if not os.path.exists(p):
                sys.exit(f"{p}: not there")
            want = (known.get(key) or {}).get(n)
            if want and not unverified:
                if isinstance(want, dict):           # {version: ...}: a from-source build, by what it says
                    got = version_in(p)
                    if got != want["version"]:
                        sys.exit(f"{p}: U-Boot {got or 'of no version found'} is not the recorded {want['version']} "
                                 f"(vm/ARTIFACTS.yml); --unverified takes it anyway")
                else:
                    got = sha256_of(p)
                    if got != want:
                        sys.exit(f"{p}: sha256 {got[:12]} is not the recorded {want[:12]} (vm/ARTIFACTS.yml); "
                                 f"--unverified takes it anyway, and a rebuild records its hashes there")
        os.makedirs(dest, exist_ok=True)
        for n in names:
            subprocess.run(["cp", "--sparse=always", "-fL", os.path.join(os.path.expanduser(src), n),
                            os.path.join(dest, n)], check=True)
    left = missing()
    print("ready" if not left else "still missing: " + ", ".join(left))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["setup", "up", "down", "fresh", "pin", "bundle"])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--sandbox", help="(setup) a directory holding u-boot and u-boot.dtb")
    ap.add_argument("--image", help="(setup) a directory holding disk.wic, zImage and rootfs.ext4")
    ap.add_argument("--unverified", action="store_true",
                    help="(setup) take pieces whose sha256 is not the recorded one (vm/ARTIFACTS.yml)")
    ap.add_argument("--version", default="1.0-sim", help="(bundle) the version the bundle carries")
    ap.add_argument("--foreign", action="store_true", help="(bundle) sign with a key the device does not trust")
    ap.add_argument("--compatible", help="(bundle) make it a bundle for another device")
    ap.add_argument("--broken", choices=sorted(BROKEN), help="(bundle) signed, accepted, and does not work")
    ap.add_argument("--registry", help="the registry the devices are read from, in place of this plugin's "
                                       "own: another plugin's device on the same machine image")
    a = ap.parse_args(argv)
    if a.action == "setup":
        return setup(a.sandbox, a.image, a.unverified)
    pidfile = os.path.join(STATE, "simvm.pid")
    if a.registry:
        # another registry's devices run beside this plugin's, each set with its own `up`
        import hashlib  # pylint: disable=import-outside-toplevel
        global REGISTRY  # pylint: disable=global-statement
        REGISTRY = os.path.abspath(a.registry)
        pidfile = os.path.join(STATE, f"simvm-{hashlib.sha256(REGISTRY.encode()).hexdigest()[:8]}.pid")
    if a.action == "down":
        try:
            with open(pidfile) as fh:
                os.kill(int(fh.read()), signal.SIGTERM)
            print("stopped")
        except (OSError, ValueError):
            print("not up")
        return None
    left = missing()
    if left:
        sys.exit("the simulated A/B bench is not set up here: " + ", ".join(left))
    known = devices()
    names = a.names or sorted(known)
    for n in names:
        if n not in known:
            sys.exit(f"{n}: not in {REGISTRY}")
    if a.action == "bundle":
        try:
            print(bundle(a.version, a.foreign, compatible=a.compatible, broken=a.broken))
        except SimError as e:
            sys.exit(str(e))
        return None
    if a.action == "pin":
        pin(names)
        print("pinned")
        return None
    if a.action == "fresh":
        for n in names:
            fresh(n)
            print(f"{n}: factory state")
        return None
    script = boot_script()
    running = [Device(n, known[n], script) for n in names]
    signal.signal(signal.SIGTERM, lambda *_: [d.end() for d in running])
    signal.signal(signal.SIGINT, lambda *_: [d.end() for d in running])
    with open(pidfile, "w") as fh:
        fh.write(str(os.getpid()))
    try:
        for d in running:
            d.start()
        print("up", flush=True)
        while any(d.is_alive() for d in running):
            time.sleep(0.5)
    finally:
        for d in running:
            d.end()
        os.unlink(pidfile)
    return None


if __name__ == "__main__":
    main()
