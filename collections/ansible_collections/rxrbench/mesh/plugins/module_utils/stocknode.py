"""A stock Meshtastic node reached through the stock library, over USB or the network.

A stock node's USB port speaks Meshtastic's own binary framing, not a console, so its board
has no serial broker (its family has no console: group `no_console`). A module opens the port
itself, the way the survey probe and the identity read do:

- `serial`: the port is resolved by name and checked (`devreg resolve`, `devreg guard`) every
  time it is opened, and locked (`devlock`) for the whole session, so nothing else opens it
  between two writes. A node restarts after some writes; its `/dev/serial/by-id` name stays
  the same, and a different one is refused.
- `tcp`: the node's address must be the one the registry records for that route
  (`devreg guard --route`), checked before the first connection.

Opening a native-USB port resets some boards (docs/HARDWARE.md, "Serial ports"), and a node restarting after a write refuses or drops a connection; `open()`
retries until its deadline.
"""
import os
import sys
import time

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from . import netconfig, stockradio


class NodeError(RuntimeError):
    pass


#: Seconds beyond the library's own timeout before an open is taken for hung.
HANG_MARGIN = 15.0


class Unanswered(NodeError):
    """The node is not there to read: devreg finds no such device on this host, or its API
    never answered this session. Nothing was exchanged, so a play skips the board."""


class Hung(NodeError):
    """The stock library did not return within its deadline: it may still hold the port, so
    the run stops here instead of opening it again."""


def with_deadline(fn, seconds, what):
    """fn() in a daemon thread, its result or its exception; Hung if it has not returned in
    `seconds` (the stock library can block where its own timeout does not reach)."""
    import threading  # pylint: disable=import-outside-toplevel
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # pylint: disable=broad-except
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise Hung(f"{what} did not return within {int(seconds)} s (the stock library hung; "
                   "the node may need a reset: unplug it or press its reset)")
    if "error" in box:
        raise box["error"]
    return box["value"]


class Session:
    """One module run's hold on a node. Use as a context manager; `open()` gives a connected
    interface (close it after each phase), `read()` the node's facts."""

    def __init__(self, transport, board, device=None, host=None, port=netconfig.PORT,
                 bearer="wifi", devreg="~/.devreg/devreg.py", devlock_dir="~/.devreg",
                 timeout=30, opener=None, guard=None, lock=None, nodes=False):
        if transport not in ("serial", "tcp"):
            raise NodeError(f"unknown transport {transport!r}")
        self.transport, self.board, self.device = transport, board, device or board
        self.host, self.port, self.bearer, self.timeout = host, port, bearer, timeout
        self.devreg, self.devlock_dir = os.path.expanduser(devreg), os.path.expanduser(devlock_dir)
        self._opener, self._guard, self._lock_factory = opener, guard, lock
        self.nodes = nodes        # download the node database too (a scenario step reads it)
        self.path, self.lock = None, None
        self.answered = False     # its API answered in this session (after that, not absent)

    # -- where the node is, and proof that it is this board --------------------------------
    def _guarded_path(self):
        if self._guard is not None:
            return self._guard(self.device)
        return common.guarded_port(self.devreg, self.device)

    def _check_route(self):
        if self._guard is not None:
            return None
        return common.route_refusal(self.devreg, self.device, self.bearer, self.host)

    def __enter__(self):
        if self.transport == "tcp":
            why = self._check_route()
            if why:
                raise NodeError(why)
            return self
        path, why = self._guarded_path()
        if isinstance(why, common.Absent):
            raise Unanswered(f"{self.board}: {why}")
        if why:
            raise NodeError(why)
        self.path = path
        self.lock = self._make_lock(path)
        if not self.lock.acquire():
            held = self.lock.holder_summary()
            self.lock.release()
            self.lock = None
            hint = ""
            if "rxrbench-broker" in str(held):
                # a board switched to stock firmware whose old family's broker still runs
                hint = (": a serial broker holds it, but stock Meshtastic has no console: "
                        "`just hosts-setup <layout>` retires the broker of a board declared a "
                        "family without one (run it after every family change)")
            raise NodeError(f"{self.board}: port busy: {held}{hint}")
        return self

    def __exit__(self, *exc):
        if self.lock is not None:
            self.lock.release()
            self.lock = None

    def _make_lock(self, path):
        if self._lock_factory is not None:
            return self._lock_factory(path)
        if self.devlock_dir not in sys.path:
            sys.path.insert(0, self.devlock_dir)
        from devlock import DevLock  # pylint: disable=import-error,import-outside-toplevel
        return DevLock(path, tool="rxrbench-stock-meshtastic", device=self.device,
                       purpose=f"phone API session with {self.board}")

    # -- a connection -------------------------------------------------------------------------
    def _connect(self):
        if self._opener is not None:
            return self._opener(self)
        if self.transport == "tcp":
            return netconfig.connect(self.host, self.port, self.timeout, nodes=self.nodes)
        from meshtastic.serial_interface import SerialInterface  # pylint: disable=import-outside-toplevel
        return SerialInterface(devPath=self.path, noNodes=not self.nodes, timeout=self.timeout)

    def open(self, deadline=60.0, sleep=time.sleep, clock=time.monotonic):
        """A connected interface whose configuration has arrived. Retries a connection refused
        or cut off (a reset on open, a restart after a write) until `deadline` seconds."""
        end = clock() + deadline
        while True:
            why = self._try_path()
            if why is None:
                iface = None
                try:
                    iface = with_deadline(self._connect, self.timeout + HANG_MARGIN,
                                          f"{self.board}: opening its phone API")
                    if getattr(iface, "myInfo", None) is None:
                        raise NodeError(f"{self.board} did not answer its phone API")
                    self.answered = True
                    return iface
                except Hung:
                    raise
                except Exception as e:  # pylint: disable=broad-except
                    # the stock library raises its own types (a refused connect, a download cut off)
                    why = f"{type(e).__name__}: {e}"
                    if iface is not None:
                        try:
                            with_deadline(iface.close, 15, f"{self.board}: closing its phone API")
                        except Exception:  # pylint: disable=broad-except
                            pass
            if clock() >= end:
                # Never answered in this session and not there: a network node that is off, a
                # USB port devreg does not find. A port that is there but whose API does not
                # answer, or a node that answered once and did not come back, is a failure.
                gone = self.transport == "tcp" or isinstance(why, common.Absent)
                kind = Unanswered if gone and not self.answered else NodeError
                raise kind(f"{self.board}: cannot open its phone API: {why}")
            sleep(2)

    def _try_path(self):
        """None when the port may be opened now, else why not (a node still restarting has no
        port yet). A node that restarts may come back on another port (a ttyACM number is not
        an identity): devreg has just guarded it as this board, so the session follows it and
        moves its lock; one another holds there is waited for like a node not back yet."""
        if self.transport != "serial":
            return None
        path, why = self._guarded_path()
        if why:
            return why
        if path != self.path:
            lock = self._make_lock(path)
            same = (self.lock is not None and getattr(lock, "lockfile", None) is not None
                    and getattr(lock, "lockfile", None) == getattr(self.lock, "lockfile", None))
            if not same:
                # Locks are keyed by the board's by-id name where there is one, so the lock
                # this session holds is usually the new path's too: it is kept (taking it
                # again would find it held, by this session). Only another key is taken.
                if not lock.acquire():
                    held = lock.holder_summary()
                    lock.release()
                    return f"{self.board} came back as {path}, which is busy: {held}"
                if self.lock is not None:
                    self.lock.release()
                self.lock = lock
            self.path = path
        return None

    def read(self, deadline=60.0):
        iface = self.open(deadline)
        try:
            return facts_of(iface)
        finally:
            iface.close()


def facts_of(iface):
    """netconfig's facts (owner, device, channels, display, security, hop limit) and the
    radio as stockradio compares it."""
    facts = netconfig.facts_of(iface)
    facts["radio"] = stockradio.facts_of(iface.localNode.localConfig.lora)
    return facts
