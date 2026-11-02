"""A simulated board: a firmware built for native_sim, run as a process whose console is on a
pty.

It is the real firmware: the same shell and the same settings store (Zephyr's, the app's config) as a board on the bench.
Its "flash memory" is a file the executable is given (`-flash=`, when the image takes it), so
stopping the process and starting it again is a reboot that keeps its config; the pty changes
at every boot, as a real board's port may. The executable itself is what a flash replaces
(`<workdir>/<name>.exe`); until something is flashed the board runs its factory image.
"""
import hashlib
import os
import re
import subprocess
import threading
import time

# a firmware naming its pty (native_sim), or QEMU naming the one its serial port is on
_PTY = re.compile(r"connected to pseudotty: (\S+)|char device redirected to (\S+) \(label serial0\)")
_OPTIONS = {}


def sha256_of(path):
    """The hash of a file: what a board of a family with no state line is known by."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def options_of(exe):
    """The command-line options a native_sim executable takes, from its --help (cached by the
    file's identity, so a reflashed board is asked again)."""
    st = os.stat(exe)
    key = (exe, st.st_mtime_ns, st.st_size)
    if key not in _OPTIONS:
        try:
            text = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=10,
                                  check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            text = ""
        # an option is -word, -word_word or -word-word (`-seed-random`)
        _OPTIONS[key] = set(re.findall(r"(?<![\w-])(-[a-z_][a-z_-]*)", text))
    return _OPTIONS[key]


class SimBoard:
    def __init__(self, workdir, exe, name="sim1", device_id=None, seed=None, console="pty"):
        """exe: the factory image; device_id: what the firmware reports as its hardware id
        (`-device_id`, when the image takes it); seed: native_sim's entropy seed. Two boards of
        a firmware that makes its identity from entropy need different seeds, or they come up
        the same. console: `pty` for a firmware that makes its own pty and names it on stdout
        (Zephyr's native_sim), `stdio` for one that speaks on its standard streams (NuttX's
        sim): then the bench owns the pty, and the board's console path is a pty of the
        bench's relayed to the firmware's streams."""
        self.workdir, self.name, self.device_id = str(workdir), name, device_id
        self.factory, self.seed, self.console = exe, seed, console
        self._bench_pty = None      # (master fd, slave path) of the bench's pty, stdio consoles
        self._relay = None
        self.flash = os.path.join(self.workdir, f"{name}.flash")
        # What a flash replaces, and how it restarts the board: the board's own executable
        # (the factory image until something is flashed) and the running process's pid.
        self.exe = os.path.join(self.workdir, f"{name}.exe")
        self.pid_path = os.path.join(self.workdir, f"{name}.pid")
        self.out_path = os.path.join(self.workdir, f"{name}.out")
        self.proc = None
        self.boots = 0
        self._pty = None

    def start(self, timeout=10.0):
        exe = self.exe if os.access(self.exe, os.X_OK) else self.factory
        args = [exe]
        # What the image running now accepts, not what the board started with: a board
        # flashed from one firmware to another restarts with the other's options (one takes a
        # flash file and a device id, another neither).
        opts = options_of(exe) if self.console != "qemu" else set()
        if "-flash" in opts:
            args.append(f"-flash={self.flash}")
        if self.device_id is not None and "-device_id" in opts:
            args.append(f"-device_id={self.device_id}")
        if self.seed is not None and "-seed" in opts:
            args.append(f"-seed={self.seed}")
        elif "-seed-random" in opts:
            # no seed declared: a board's entropy is its own at every boot, as a chip's is,
            # so a key the firmware makes is not the same key again after an erase
            args.append("-seed-random")
        # pylint: disable=consider-using-with
        out = open(self.out_path, "ab")
        mark = out.tell()
        if self.console == "stdio":
            return self._start_on_stdio(args, out)
        if self.console == "qemu":
            args = self._qemu_argv()
        self.proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, cwd=self.workdir)
        out.close()
        with open(self.pid_path, "w") as f:
            f.write(str(self.proc.pid))
        self.boots += 1
        self._pty = None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with open(self.out_path, "rb") as f:
                f.seek(mark)
                m = _PTY.search(f.read().decode(errors="replace"))
            if m:
                self._pty = m.group(1) or m.group(2)
                return self
            if self.proc.poll() is not None:
                raise RuntimeError(f"the sim board exited at once (rc {self.proc.returncode})")
            time.sleep(0.02)
        raise RuntimeError("the sim board did not name its console")

    def _qemu_argv(self):
        """An emulated MCU: the "executable" is the image directory's manifest, whose `qemu`
        block says the machine and what QEMU loads (the bootloader as the kernel, the
        application placed in its slot by QEMU's loader before the first instruction). The
        board's flash is the emulated machine's RAM: kept across a CPU reset, gone with the
        process, so a power cycle boots the image staged here again."""
        import json  # pylint: disable=import-outside-toplevel
        image = os.path.dirname(self.factory)
        with open(os.path.join(image, "manifest.json")) as f:
            q = json.load(f)["qemu"]
        argv = [q.get("system", "qemu-system-arm"), *q["argv"], "-nographic", "-monitor", "none", "-serial", "pty",
                "-kernel", os.path.join(image, q["kernel"])]
        for item in q.get("load", []):
            argv += ["-device", f"loader,file={os.path.join(image, item['file'])},addr={item['addr']}"]
        return argv

    def _start_on_stdio(self, args, out):
        """The firmware's standard streams on a pty of their own, relayed to the bench's pty
        (made once, kept across reboots: the console path a board's broker opens). What the
        firmware writes with nobody listening is dropped, as a UART's bytes would be."""
        import pty as _pty  # pylint: disable=import-outside-toplevel
        import select  # pylint: disable=import-outside-toplevel
        import tty  # pylint: disable=import-outside-toplevel
        if self._bench_pty is None:
            bm, bs = _pty.openpty()
            tty.setraw(bs)
            os.set_blocking(bm, False)
            self._bench_pty = (bm, os.ttyname(bs))
            os.close(bs)
        fm, fs = _pty.openpty()
        tty.setraw(fs)
        self.proc = subprocess.Popen(args, stdin=fs, stdout=fs, stderr=out, cwd=self.workdir, close_fds=True)
        os.close(fs)
        out.close()
        bm = self._bench_pty[0]

        def relay(fm=fm, proc=self.proc):
            try:
                while proc.poll() is None:
                    r, _, _ = select.select([fm, bm], [], [], 0.2)
                    for src, dst in ((fm, bm), (bm, fm)):
                        if src in r:
                            try:
                                data = os.read(src, 4096)
                            except OSError:
                                data = b""
                            if not data and src == fm:
                                return
                            try:
                                if data:
                                    os.write(dst, data)
                            except (BlockingIOError, OSError):
                                pass        # nobody is listening on that side
            finally:
                os.close(fm)
        self._relay = threading.Thread(target=relay, name=f"{self.name}-relay", daemon=True)
        self._relay.start()
        with open(self.pid_path, "w") as f:
            f.write(str(self.proc.pid))
        self.boots += 1
        self._pty = self._bench_pty[1]
        time.sleep(0.05)
        if self.proc.poll() is not None:
            raise RuntimeError(f"the sim board exited at once (rc {self.proc.returncode})")
        return self

    def path(self):
        """The console's current path; None while the board is down. If the firmware
        rebooted itself (the process exited), this boots it again first."""
        if self.proc is not None and self.proc.poll() is not None:
            self.start()
        return self._pty if self.running() else None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._pty = None

    def reboot(self):
        """Power off and on: the flash file stays."""
        self.stop()
        return self.start()

    def output(self):
        with open(self.out_path, "rb") as f:
            return f.read().decode(errors="replace")
