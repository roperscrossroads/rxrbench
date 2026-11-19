"""Talk to a board through its serial broker (see host/broker/broker.py)."""
import json
import os
import re
import socket

# Commands whose text carries a secret, by shape, so that an error names a command by its
# words and never by its payload. A plugin's module_utils adds its consoles' shapes when it is
# imported (secret_shapes): a payload pattern (group 1 the words, group 2 the payload, group 3
# what follows), or a prefix after which everything is the secret.
_PAYLOADS = []
_PREFIXES = []


def secret_shapes(payloads=(), prefixes=()):
    """Add shapes: `payloads` are regular expressions as above, `prefixes` command words."""
    _PAYLOADS.extend(p for p in map(re.compile, payloads) if p.pattern not in
                     [q.pattern for q in _PAYLOADS])
    _PREFIXES.extend(w for w in prefixes if w not in _PREFIXES)


def shown(cmd):
    """`cmd` as an error message may quote it."""
    for pattern in _PAYLOADS:
        m = pattern.match(cmd)
        if m:
            return f"{m.group(1)}<{len(m.group(2))} chars>"
    for w in _PREFIXES:
        if cmd.startswith(w):
            return w + "<secret>"
    return cmd


def secret_of(cmd):
    """The secret a command carries, when its shape says it has one (else None)."""
    return next((cmd[len(w):] for w in _PREFIXES if cmd.startswith(w)), None) or None


class BrokerError(Exception):
    pass


def socket_path(board, runtime_dir=None):
    runtime_dir = runtime_dir or os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime_dir, "rxrbench", f"{board}.sock")


class NoBroker:
    """The board of a family with no console (stock firmware asked over its own protocol): no
    broker owns its port. Nothing to release or take back; a console command is refused, so
    asking it as a console family says it is not one."""

    def __init__(self, board, path=None, **_kw):
        self.board, self.path = board, path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def release(self, ttl=None):  # pylint: disable=unused-argument
        return {"ok": True}

    def reacquire(self):
        return {"ok": True}

    def exec(self, *_a, **_kw):
        raise BrokerError(f"{self.board}: no broker (its firmware has no console)")


def board_or_none(board, path=None, **kw):
    """The board's broker when it has one (its socket exists), else NoBroker: a board whose
    firmware has no console (hosts-setup retires its broker) is still read and written whole
    through its chip's loader, which needs no console, only nobody else on the port."""
    path = path or socket_path(board)
    return Board(board, path=path, **kw) if os.path.exists(path) else NoBroker(board, path)


def opens(board):
    """How many times the broker has opened the board's port, or None when that cannot be
    asked (no broker, a fake in a test, a broker that does not say). A reboot wait counts a rise
    as the console having gone down: a command finds a closed port reopened for it at once
    (`ensure_open`), so it no longer fails while the board is away."""
    try:
        n = board.status().get("opens")
    except Exception:  # noqa: BLE001 - a board that cannot say is judged by its uptime alone
        return None
    return n if isinstance(n, int) and not isinstance(n, bool) else None


def reopened(board, before):
    """Has the broker reopened the port since it counted `before` opens?"""
    if before is None:
        return False
    now = opens(board)
    return now is not None and now > before


class Board:
    """One connection to one board's broker. Use as a context manager."""

    def __init__(self, board, path=None, timeout=60.0, busy_wait=120.0):
        self.board = board
        self.path = path or socket_path(board)
        self.timeout = timeout
        self.busy_wait = busy_wait
        self.sock = None
        self.buf = b""
        self.fresh = False

    def _connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.buf = b""
        try:
            self.sock.connect(self.path)
        except OSError as e:
            raise BrokerError(f"{self.board}: no broker at {self.path} ({e}). A board whose "
                              "firmware has a console is reached through its broker: `just "
                              "hosts-setup <layout>` starts one for each such board (after "
                              "enrolling it, or declaring it another firmware family)") from e
        self.fresh = True

    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, *exc):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _once(self, req):
        self.sock.sendall(json.dumps(req).encode() + b"\n")
        while b"\n" not in self.buf:
            data = self.sock.recv(65536)
            if not data:
                raise BrokerError(f"{self.board}: the broker closed the connection")
            self.buf += data
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def request(self, **req):
        """One request. A broker serves one client at a time and turns a second one away
        ("busy") when it connects: then this connection waits its turn (another task on the
        same board, another board's step reading it) instead of failing."""
        import time  # pylint: disable=import-outside-toplevel
        end, pause = time.monotonic() + self.busy_wait, 0.25
        while True:
            try:
                r = self._once(req)
            except (BrokerError, OSError) as e:
                if not self.fresh:
                    if isinstance(e, BrokerError):
                        raise
                    # The broker hung up on this connection after an earlier request (a
                    # write timeout on a board that takes no input closes it): say so as the
                    # broker's error, which the callers handle, not as a bare socket error.
                    raise BrokerError(f"{self.board}: the connection to the broker was "
                                      f"lost ({e})") from e
                # The first request on a new connection, closed by the broker: it said
                # "busy" and hung up, and the close was seen before the word.
                r = {"ok": False, "error": "busy"}
            if not (self.fresh and r.get("ok") is False and r.get("error") == "busy") \
                    or time.monotonic() >= end:
                self.fresh = False
                return r
            self.sock.close()
            time.sleep(pause)
            pause = min(pause * 2, 5.0)
            self._connect()

    def exec(self, cmd, deadline=8.0, dialect=None, expect_reply=True, secret=None):
        """Run one console command and return its complete output, or raise. `dialect`
        names a one-line console the broker has rules for (a family's `console`).
        `secret` names a secret inside `cmd` (found by its shape when not given): the broker
        keeps it out of its log and of what it answers, wherever the board repeats it."""
        extra = {"dialect": dialect, "expect_reply": expect_reply} if dialect else {}
        secret = secret or secret_of(cmd)
        if secret:
            extra["secret"] = secret
        r = self.request(op="exec", cmd=cmd, deadline=deadline, **extra)
        if not r.get("ok"):
            raise BrokerError(f"{self.board}: `{shown(cmd)}`: {r.get('error')}")
        return r["output"]

    def status(self):
        return self.request(op="status")

    def release(self, ttl=120.0):
        try:
            r = self.request(op="release", ttl=ttl)
        except (BrokerError, OSError):
            # The broker hung up on this connection since its last request: a read before
            # this one timed out on a board that takes no input (MCUboot with no app, after
            # `just boot` from another family). Lending the port is idempotent, so ask again
            # on a fresh connection.
            if self.sock is not None:
                self.sock.close()
            self._connect()
            r = self.request(op="release", ttl=ttl)
        if not r.get("ok"):
            raise BrokerError(f"{self.board}: release: {r.get('error')}")
        return r["port"]

    def reacquire(self):
        return self.request(op="reacquire")
