#!/usr/bin/env python3
"""Advisory locking for serial devices, keyed on stable hardware identity.

The problem
-----------
Several tools open the same board's serial port. Nothing coordinated them, and
the two failure modes are not equally visible:

  * Some serial tools pass ``exclusive=True`` to pyserial, so a second opener
    gets ``Could not exclusively lock port /dev/ttyACM0``, which reads like a
    missing board.
  * pyserial's default takes no lock and raises nothing, so two readers split
    the byte stream and neither errors: a split stream presents as a flaky board.

This module gives both cases one lock, and answers "who has it?".

Why flock, and why there is nothing to reap
-------------------------------------------
The lock is ``flock(LOCK_EX|LOCK_NB)`` on a file under the user's runtime dir.
An flock lives on an open file descriptor, and the kernel drops it when the
holder dies (cleanly, by signal, or by OOM kill), so there are no stale locks:
no PID to check for liveness, no timeout, no reaper.

A leftover lock file is not a lock. PID files or expiry would have to guess
whether a holder is alive.

The JSON inside the file is metadata for humans, never consulted for the
decision. The file-descriptor test is truth; the JSON is a hint that may be
stale, and the reporting says so when the two disagree.

Why the key is the by-id basename
---------------------------------
Not the ``/dev/ttyACM*`` name, which renumbers on replug, and not the devreg
name. The ``/dev/serial/by-id/`` basename embeds the chip serial, so every host
derives the same key for the same physical board without a registry lookup
(``uf2_flash.py`` keys its board lock on the same chip serial). A lock taken by a tool that does not
use devreg therefore interlocks with one taken by a tool that does; locking does
not depend on the registry. The exception is a USB device that reports no serial number
(a CH340): every bridge of its make shares one by-id name, which udev moves to the last one
plugged in, so its key is the USB port it is on (``/dev/serial/by-path/``).

Usage
-----
    from devlock import DevLock, probe

    with DevLock(port, tool="monitor", expect_long_hold=True) as lk:
        if not lk.held:
            sys.exit(f"busy: {lk.holder_summary()}")
        ...
"""
from __future__ import annotations

import errno
import fcntl
import glob
import getpass
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["lock_key", "lock_path", "DevLock", "probe", "all_holders", "open_by"]

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _lockdir() -> Path:
    """Runtime dir, which is tmpfs, so a lock cannot survive a reboot.

    Every tool on the host must resolve the same directory, or two tools do not interlock:
    DEVLOCK_DIR when set; else the user's runtime dir, from XDG_RUNTIME_DIR or, for a process
    started without it (cron, a bare ssh command), /run/user/<uid> where logind keeps it.
    /tmp is only for a host with no runtime dir at all.
    """
    explicit = os.environ.get("DEVLOCK_DIR")
    if explicit:
        d = Path(os.path.expanduser(explicit))
    else:
        run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        d = (Path(run) if os.path.isdir(run) else Path(f"/tmp/devlock-{os.getuid()}")) / "devlock"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


RESOURCE = re.compile(r"^[a-z][a-z0-9]*:[A-Za-z0-9_.-]+$")


def is_resource(path: str) -> bool:
    """A named resource rather than a device node: ``<kind>:<name>``, e.g. ``ble:hci0``.

    A bench host has one Bluetooth adapter, and every BLE tool on it (a flash over MCUmgr, a
    config write through the stock library, a bonding agent's round trip) needs it exclusively
    for seconds to minutes; two at once break both (docs/HARDWARE.md, "Bluetooth LE and
    MCUmgr"). There is no device node to flock, so the adapter is locked by name: same
    mechanism, same directory, same `who`; the key is the name, sanitised.
    """
    return bool(RESOURCE.match(path))


def _links_to(target: str, d: Path) -> list[str]:
    """The names in directory @d that are links to device node @target."""
    out = []
    if d.is_dir():
        for link in d.iterdir():
            try:
                if os.path.realpath(link) == target:
                    out.append(link.name)
            except OSError:
                continue
    return out


def _usb_serial_less(target: str, sys_tty: str) -> bool:
    """True when the tty @target is on a USB device that reports no serial number.

    Such a bridge (a CH340 on many boards) gets a by-id name with no serial in it, the same
    name for every bridge of its make, and udev points that one name at whichever of them
    came last: two boards would share one lock, and a holder of the name would hold the
    other board's port after a replug. False when it cannot tell (not USB, no sysfs).
    """
    dev = os.path.join(sys_tty, os.path.basename(target), "device")
    if not os.path.exists(dev):
        return False
    d = os.path.realpath(dev)
    while d not in ("/", "") and not os.path.exists(os.path.join(d, "idVendor")):
        d = os.path.dirname(d)
    if not os.path.exists(os.path.join(d, "idVendor")):
        return False
    return not os.path.exists(os.path.join(d, "serial"))


def lock_key(path: str, by_id: str = "/dev/serial/by-id", by_path: str = "/dev/serial/by-path",
             sys_tty: str = "/sys/class/tty") -> str:
    """Stable key for the board behind @path, or for a named resource.

    Prefers the /dev/serial/by-id/ name that points at the same device node, so
    /dev/ttyACM1 and its by-id symlink lock each other out: different tools reach the
    same board by different names. A USB device with no serial number has no by-id name of
    its own (every bridge of its make shares one), so its key is its /dev/serial/by-path/
    name instead: the USB port it is plugged into, the same for every tool on the host.
    """
    if is_resource(path):
        return _SAFE.sub("_", path)
    target = os.path.realpath(path)
    names = []
    if _usb_serial_less(target, sys_tty):
        names = _links_to(target, Path(by_path))
    names = names or _links_to(target, Path(by_id))
    if names:
        return _SAFE.sub("_", min(names))
    return _SAFE.sub("_", os.path.basename(target) or target)


def lock_path(path: str) -> Path:
    return _lockdir() / (lock_key(path) + ".lock")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_meta(p: Path) -> dict:
    try:
        return json.loads(p.read_text() or "{}")
    except (OSError, ValueError):
        return {}


def _age(since: str | None) -> str:
    if not since:
        return "unknown"
    try:
        t = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "unknown"
    secs = int((datetime.now(timezone.utc) - t).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _pid_alive(pid: int | None) -> bool | None:
    """None when it cannot be told (no Linux /proc, or no permission)."""
    if not pid:
        return None
    try:
        return Path(f"/proc/{pid}").exists()
    except OSError:
        return None


class DevLock:
    """Context manager holding the advisory lock for one device."""

    def __init__(self, path: str, tool: str, *, device: str | None = None,
                 purpose: str | None = None, expect_long_hold: bool = False):
        self.path = path
        self.tool = tool
        self.device = device
        self.purpose = purpose
        self.expect_long_hold = expect_long_hold
        self.lockfile = lock_path(path)
        self._fd: int | None = None
        self.held = False

    def acquire(self, wait_s: float = 0.0) -> bool:
        """Take the lock. @wait_s > 0 retries until the deadline."""
        deadline = time.monotonic() + wait_s
        self._fd = os.open(self.lockfile, os.O_RDWR | os.O_CREAT, 0o644)
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                self._stamp()
                return True
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.25)

    def _stamp(self) -> None:
        """Record the holder. Written after acquiring, so it describes the current holder,
        not the last one to try."""
        meta = {
            "pid": os.getpid(),
            "user": getpass.getuser(),
            "host": socket.gethostname(),
            "tool": self.tool,
            "device": self.device,
            "path": self.path,
            "since": _now(),
            "expect_long_hold": self.expect_long_hold,
        }
        if self.purpose:
            meta["purpose"] = self.purpose
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, (json.dumps(meta) + "\n").encode())
            os.fsync(self._fd)
        except OSError:
            pass  # metadata is a courtesy; the lock is the contract

    def holder_summary(self) -> str:
        return describe(probe(self.path))

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None
        self.held = False

    def __enter__(self) -> "DevLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def open_by(path: str) -> list[dict]:
    """Processes holding @path's device node open, found via /proc.

    devlock's own lockfile sees only tools that use it. pyserial's ``exclusive=True``,
    which some serial tools use, takes its flock on the device node itself, so a tool
    holding a port that way is invisible to the lockfile; /proc shows it.

    Read-only, and it does not open the device: opening a port can reset the board
    (docs/HARDWARE.md, "Serial ports").

    Only same-user processes are reliably visible; that is the case that matters
    here, and the caller is told when a port is held by nobody it can see.
    """
    if is_resource(path):
        return []   # nothing to open: a named resource is only ever held by its lock
    target = os.path.realpath(path)
    out: dict[int, dict] = {}
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.path.realpath(fd) != target:
                continue
            pid = int(fd.split("/")[2])
            if pid == os.getpid():
                continue
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace").strip()
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            out[pid] = {"pid": pid, "comm": comm, "cmdline": cmd}
        except (OSError, ValueError, IndexError):
            continue
    return sorted(out.values(), key=lambda d: d["pid"])


def probe(path: str) -> dict | None:
    """Who holds @path's lock, or None if free.

    Held-ness is decided by trying to take the lock, never by reading the JSON:
    a lockfile lying around with plausible contents means nothing if no process
    holds a descriptor on it.
    """
    p = lock_path(path)
    if not p.exists():
        return None
    try:
        fd = os.open(p, os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None  # the lock was free
    except OSError as e:
        if e.errno not in (errno.EACCES, errno.EAGAIN):
            return None
        meta = _read_meta(p)
        meta["_lockfile"] = str(p)
        meta["_age"] = _age(meta.get("since"))
        meta["_pid_alive"] = _pid_alive(meta.get("pid"))
        return meta
    finally:
        os.close(fd)


def describe(meta: dict | None) -> str:
    """One line naming the holder, or 'free'."""
    if meta is None:
        return "free"
    tool = meta.get("tool") or "an unnamed tool"
    pid = meta.get("pid", "?")
    who = f"{meta.get('user', '?')}@{meta.get('host', '?')}"
    s = f"{tool} (pid {pid}, {who}) since {meta.get('since', '?')} ({meta['_age']})"
    if meta.get("expect_long_hold"):
        s += " [long hold declared]"
    # The JSON and the lock disagree: something holds the lock, and it is not the holder
    # the metadata names.
    if meta.get("_pid_alive") is False:
        s += (f" -- WARNING: pid {pid} is gone, so the holder is NOT what this "
              f"says; something holds the lock without metadata")
    return s


def all_holders() -> list[dict]:
    """Every held lock on this host, whether or not the device is registered."""
    out = []
    for p in sorted(_lockdir().glob("*.lock")):
        meta = _read_meta(p)
        path = meta.get("path") or str(p)
        held = probe(path) if meta.get("path") else None
        if held is None:
            # Fall back to probing the lockfile itself: a holder that never
            # wrote metadata still has to be reported.
            try:
                fd = os.open(p, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                continue  # free
            except OSError:
                held = dict(meta)
                held["_lockfile"] = str(p)
                held["_age"] = _age(meta.get("since"))
                held["_pid_alive"] = _pid_alive(meta.get("pid"))
            finally:
                os.close(fd)
        held["_key"] = p.stem
        out.append(held)
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────
# A thin command surface so shell callers (the justfile) can use the lock in one-liners:
# a multi-line `python3 -c` inside a just recipe breaks its parser.

def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="devlock", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("path", help="print the lockfile path for a device")
    p.add_argument("device")

    p = sub.add_parser("who", help="describe the holder, or 'free'")
    p.add_argument("device")

    p = sub.add_parser("stamp", help="write holder metadata into an already-held lockfile")
    p.add_argument("device")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--tool", required=True)
    p.add_argument("--name", default=None, help="devreg name of the board")
    p.add_argument("--purpose", default=None)

    # `run`: hold the lock for the length of a command. For a tool that reaches the host
    # over ssh and cannot take the lock in-process: `devlock.py run ble:hci0 --wait 300
    # --tool mytool -- smpmgr
    # --ble ...`. The command inherits the terminal; its exit status is returned; the lock
    # is dropped with the descriptor when either process dies.
    p = sub.add_parser("run", help="hold a device's or resource's lock around a command")
    p.add_argument("device", help="a device path, or a resource such as ble:hci0")
    p.add_argument("--tool", required=True)
    p.add_argument("--wait", type=float, default=0.0,
                   help="seconds to wait for a busy lock (default: fail at once)")
    p.add_argument("--name", default=None, help="devreg name of the board")
    p.add_argument("--purpose", default=None)

    # The command is everything after `--`, split off here: a REMAINDER positional would
    # swallow the options that follow the device name.
    cmd: list[str] = []
    if argv[:1] == ["run"] and "--" in argv:
        cut = argv.index("--")
        argv, cmd = argv[:cut], argv[cut + 1:]

    a = ap.parse_args(argv)
    if a.cmd == "run":
        import subprocess
        if not cmd:
            ap.error("run: no command given (put it after --)")
        lk = DevLock(a.device, tool=a.tool, device=a.name, purpose=a.purpose)
        if not lk.acquire(wait_s=a.wait):
            print(f"busy: {a.device} is held by {lk.holder_summary()}", file=sys.stderr)
            return 75   # EX_TEMPFAIL: try again later
        try:
            return subprocess.call(cmd)
        finally:
            lk.release()
    if a.cmd == "path":
        print(lock_path(a.device))
        return 0
    if a.cmd == "who":
        meta = probe(a.device)
        if meta is None:
            openers = open_by(a.device)
            if openers:
                detail = "; ".join(
                    f"pid {o['pid']} {o['comm']}: {o['cmdline'][:60]}" for o in openers)
                print(f"held without a devlock -- {detail}")
                return 1
            print("free")
            return 0
        print(describe(meta))
        return 1
    if a.cmd == "stamp":
        # The caller holds the flock on its own fd; this writes only the metadata and does
        # not take the lock itself.
        meta = {
            "pid": a.pid, "user": getpass.getuser(), "host": socket.gethostname(),
            "tool": a.tool, "device": a.name, "path": a.device,
            "since": _now(), "expect_long_hold": False,
        }
        if a.purpose:
            meta["purpose"] = a.purpose
        try:
            lock_path(a.device).write_text(json.dumps(meta) + "\n")
        except OSError:
            return 0  # metadata is a courtesy; never fail a flash over it
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
