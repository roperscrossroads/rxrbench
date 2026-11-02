#!/usr/bin/env python3
"""rxrbench serial broker: the one process that owns a board's console.

Runs on a bench host, one instance per board. It keeps the serial port open, appends
everything the board says to a log, and serves a Unix socket where one client at a time can run
a shell command and get its complete output back. That replaces a capture reader that had to be
killed and restarted around every command, and fixed sleeps as the definition of "done".

Protocol: JSON, one object per line, on the socket.

    {"op": "exec", "cmd": "kernel uptime", "deadline": 8}
    {"op": "exec", "cmd": "get radio", "dialect": "<name>"}   a console that answers with one
                                    line, by the rules a plugin gave the broker (load_dialects)
    {"op": "exec", "cmd": "password hunter22x", "secret": "hunter22x", ...}   the secret in the
                                    command: kept out of the log and the answer, wherever the
                                    board repeats it
        -> {"ok": true, "output": "...", "elapsed": 0.31}
        -> {"ok": false, "error": "no echo|timeout|port closed|...", "output": "partial"}
    {"op": "release", "ttl": 120}   close the port (for a flash); reopened after ttl at the latest
        -> {"ok": true, "port": "/dev/..."}
    {"op": "reacquire"}             reopen now
    {"op": "attend", "dialect": "<name>", "ttl": 30}   for ttl seconds, answer the first line
                                    the board prints that matches one of the dialect's prompts
                                    with what the prompt says (a boot banner waiting for a key),
                                    or with "answer" when the request carries one;
                                    ttl 0 stops attending
    {"op": "reset"}                 pulse the port's modem lines the way a flashing tool resets
                                    an ESP32 (DTR low, RTS asserted, then released): a power-on
                                    for a board whose bootloader tells those apart. The lines
                                    end where the board's rest state says (--modem-rest).
    {"op": "status"}                -> {"ok": true, "board": ..., "port": ..., "open": true, ...}
    {"op": "mark", "text": "..."}   write "### rxrbench <utc> <board>: <text>" into the log: a
                                    marker (a reflash, a test step) that a reader can find with
                                    `grep '^### '`, written by the log's one writer

How a command is read to completion, without a stopwatch:

1. The command is typed in chunks of at most CHUNK bytes, each sent only after the previous one
   was echoed. A Zephyr shell's receive ring is 64 bytes by default and silently drops a longer burst.
2. A marker line that is not a command (`mb<nonce>`) is sent straight after. The shell runs
   lines in order, so its complaint about the marker appears only when the command has
   finished. Everything between the command's echo and the marker's echo is the output.
   Waiting for "the next prompt" is not enough: a log line arriving mid-command redraws the
   prompt.
"""
import argparse
import json
import os
import re
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import time

CHUNK = 48
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
NOT_FOUND = ": command not found"

class Dialect:
    """A console that answers a command with one line (the `line` mode), as data: the pattern
    that finds the answer (its first group that matched is the answer), and the command words
    after which the firmware echoes nothing because a secret follows.

    `echo`: the console echoes what is typed (the default; a command is paced by its echo).
    One that echoes nothing is written at once and read for its answer alone. `eol`: what ends
    a typed line (a carriage return by default; a console that reads only newlines says so).
    `prompts`: lines the board prints that a client may ask the broker to answer while it is
    attending ({pattern, send}): a boot banner that waits for a key, answered once.
    `blank_reply`: what the console answers to the blank line the broker sends first to clear a
    half-typed one (a console that answers it, with an "unknown command" of its own); the
    broker waits for that answer before the command, so it is never taken for the command's."""

    def __init__(self, name, reply, secret_prefixes=(), echo=True, eol="\r", prompts=(),
                 blank_reply=None):
        self.blank_reply = re.compile(blank_reply) if blank_reply else None
        self.name, self.secret_prefixes = name, tuple(secret_prefixes)
        self.reply = reply if hasattr(reply, "search") else re.compile(reply)
        self.echo, self.eol = bool(echo), eol
        self.prompts = [(re.compile(pr["pattern"]), pr["send"]) for pr in prompts]

    def secret_of(self, cmd):
        return next((cmd[len(w):] for w in self.secret_prefixes if cmd.startswith(w)), "")


#: The line dialects this broker knows, by the name a request gives: one per file in a
#: dialects directory (load_dialects).
DIALECTS = {}
#: The log rules from the dialects directories: (pattern, label). The pattern's group `s` is
#: what the log must not keep.
SCRUBS = []
MARKER = (None, "", "zephyr-shell")      # the names that mean the default, marker-read console


def load_dialects(directory):
    """Read `<directory>/*.json`, one console's rules per file, named by the file:

        {"reply": "<regex>", "secret_prefixes": ["password "],      a line dialect (optional)
         "log_scrub": [{"pattern": "~K(?P<s>[A-Za-z0-9+/]{8,})", "label": "key"}]}

    `log_scrub` holds for every line the board prints, whatever dialect a request names:
    a client that forgets to name its secret must not get it into the log. A file that
    cannot be read is an error at start, not a console that silently logs secrets."""
    if not directory or not os.path.isdir(directory):
        return
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(directory, fname)) as fh:
            doc = json.load(fh)
        name = fname[:-5]
        if doc.get("reply"):
            for pr in doc.get("prompts") or []:
                if not (pr.get("pattern") and pr.get("send")):
                    raise ValueError(f"{fname}: a prompt needs a pattern and what to send")
            DIALECTS[name] = Dialect(name, doc["reply"], doc.get("secret_prefixes") or (),
                                     echo=doc.get("echo", True), eol=doc.get("eol", "\r"),
                                     prompts=doc.get("prompts") or (),
                                     blank_reply=doc.get("blank_reply"))
        for rule in doc.get("log_scrub") or []:
            pattern = re.compile(rule["pattern"].encode())
            if "s" not in pattern.groupindex:
                raise ValueError(f"{fname}: a log_scrub pattern needs a group named s")
            SCRUBS.append((pattern, (rule.get("label") or "").encode()))


def load_default_dialects():
    """The rules every broker starts with: `dialects.d` beside this file (where the role puts
    the plugins' files), then the directories RXRBENCH_DIALECTS names (colon-separated: the
    simulated bench and the tests, which run the broker from the source tree)."""
    load_dialects(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dialects.d"))
    for d in filter(None, (os.environ.get("RXRBENCH_DIALECTS") or "").split(os.pathsep)):
        load_dialects(d)


load_default_dialects()


def strip_ansi(text):
    return ANSI.sub("", text)


def squash(text):
    """Text with line breaks removed: what a long echoed command looks like once the shell's
    own line wrapping is taken out. Returns (squashed, index_map) where index_map[i] is the
    position in `text` of squashed[i]."""
    out, idx = [], []
    for i, ch in enumerate(text):
        if ch not in "\r\n":
            out.append(ch)
            idx.append(i)
    return "".join(out), idx


# A board that stops reading its console (crashed, erased, held in its ROM loader) lets the
# USB buffer fill, and a write without a timeout then waits for ever: the broker, which serves
# one thing at a time, stops answering everyone (docs/HARDWARE.md, "Serial ports"). A
# write that cannot finish in this time closes the port instead, and the next open retries.
WRITE_TIMEOUT = 2.0
#: Where a port's DTR and RTS rest while the broker holds it, and where a reset leaves them.
#: `asserted`, both: a terminal at rest. Deasserted at open they strap an ESP32-S3's native
#: USB port into ROM download mode across every reset, and one asserted alone pulls IO0 or EN
#: through a UART bridge's auto-reset circuit. `deasserted`, both: a bridge whose RTS drives
#: EN with no such circuit holds the chip in reset for as long as RTS is asserted (a CH340K
#: on an ESP32-S3 handheld). The board's type says which (catalog/boards.yml `modem_rest`).
MODEM_RESTS = ("asserted", "deasserted")


def rest_modem_lines(ser, rest):
    """Both lines to the board's rest state: RTS first, since RTS alone may hold EN."""
    on = rest == "asserted"
    ser.rts = on
    ser.dtr = on


MAX_DEADLINE = 600.0     # the longest one command may hold the console, in seconds
# A request names the secret its command carries (`secret`), and the broker takes it out of
# everything it keeps or hands back: the log and the answer, wherever the board repeats it.
# That a firmware does not echo a secret is its contract, not something to rely on. The
# secret is remembered this long after the command, for an echo or a redraw that comes late.
SECRET_TTL = 60.0
REDACTED = "<secret>"


#: Seconds a command waits for a closed port to open (ensure_open): a board rebooting into its
#: application enumerates within a few.
OPEN_WAIT = 5.0


class PortError(Exception):
    pass


def scrub(line):
    """A console line as the log may keep it: what a log rule names replaced by its length."""
    for pattern, label in SCRUBS:
        line = pattern.sub(lambda m, label=label: m.group(0).replace(
            m.group("s"), b"<%s%d chars, not logged>" % (label + b", " if label else b"",
                                                         len(m.group("s")))), line)
    return line


class ScrubbedLog:
    """Hands complete lines through scrub() to the log. A partial line is held until its
    newline, or until flush() lets it out (the broker does after a second: a prompt has no
    newline), so a payload split across two reads is still seen whole."""

    HOLD_MAX = 64 << 10

    def __init__(self, log):
        self.log, self.held, self.since = log, b"", 0.0
        self.secrets = {}        # a named secret (bytes) -> when it may be forgotten

    def hide(self, secret, ttl=SECRET_TTL):
        """Keep `secret` out of the log from now on, for `ttl` seconds."""
        if secret:
            self.secrets[secret.encode()] = time.monotonic() + ttl

    # What a shell may put between two bytes of a secret as it echoes it: a line wrap at the
    # terminal's width (a carriage return and a newline), an escape sequence (colour, cursor).
    GAP = re.compile(rb"\r+\n?|\n|\x1b\[[0-9;?]*[A-Za-z]")     # NSH wraps with \r\r\n

    def _live(self):
        if self.secrets:
            now = time.monotonic()
            self.secrets = {s: t for s, t in self.secrets.items() if t > now}
        return self.secrets

    @classmethod
    def _plain(cls, data):
        """`data` with the gaps a shell puts into an echo (GAP) taken out, and for each byte
        kept, its offset in `data`. Linear: a pattern with a gap allowed between every two
        bytes of a secret backtracks without end on a shell's output."""
        plain, at, i = bytearray(), [], 0
        while i < len(data):
            m = cls.GAP.match(data, i)
            if m and m.end() > i:
                i = m.end()
                continue
            plain.append(data[i])
            at.append(i)
            i += 1
        return bytes(plain), at

    def scrub(self, data):
        data = scrub(data)
        live = self._live()
        if not live:
            return data
        for s in live:
            data = data.replace(s, REDACTED.encode())
        if not any(c in data for c in (b"\r", b"\n", b"\x1b")):
            return data
        # a secret the shell wrapped or coloured: found with the gaps taken out, cut from
        # the data with them in
        plain, at = self._plain(data)
        spans = []
        for s in live:
            start = plain.find(s)
            while start != -1:
                spans.append((at[start], at[start + len(s) - 1] + 1))
                start = plain.find(s, start + len(s))
        for lo, hi in sorted(spans, reverse=True):
            data = data[:lo] + REDACTED.encode() + data[hi:]
        return data

    def write(self, data):
        continuing = bool(self.held)
        data = self.held + data
        self.held = b""
        if self._live():
            # before the split into lines: a secret echoed across a line wrap is one match,
            # and what may be the start of one (even past a line break) is not written yet
            data = self.scrub(data)
            keep = self._straddle(data)
            if keep:
                data, self.held = data[:len(data) - keep], data[len(data) - keep:]
        *lines, tail = data.split(b"\n")
        if lines:
            continuing = False
            self.log.write(b"".join(self.scrub(l) + b"\n" for l in lines))
        if len(tail) >= self.HOLD_MAX:
            self.log.write(self.scrub(tail))
        elif tail:
            self.held = tail + self.held
        if self.held and not continuing:
            self.since = time.monotonic()

    def _straddle(self, data):
        """How much of the end of `data` may be the beginning of a hidden secret: that much is
        not written yet, or a secret that arrives slowly (a long line echoed as it is paced in)
        would be logged in pieces no scrub matches. Gaps (GAP) inside the tail do not count."""
        longest = max((len(s) for s in self.secrets), default=0)
        tail = data[-(2 * longest + 64):]
        plain, at, i = b"", [], 0
        while i < len(tail):
            m = self.GAP.match(tail, i)
            if m and m.end() > i:
                i = m.end()
                continue
            plain += tail[i:i + 1]
            at.append(i)
            i += 1
        keep = 0
        for s in self.secrets:
            for k in range(min(len(s) - 1, len(plain)), 0, -1):
                if plain.endswith(s[:k]):
                    keep = max(keep, len(tail) - at[len(plain) - k])
                    break
        return keep

    def flush(self, older_than=0.0):
        if self.held and time.monotonic() - self.since >= older_than:
            keep = self._straddle(self.held) if self._live() else 0
            out, self.held = self.held[:len(self.held) - keep], self.held[len(self.held) - keep:]
            if out:
                self.log.write(self.scrub(out))
            if self.held:
                self.since = time.monotonic()


class RotatingLog:
    """An append-only console log that never grows past `max_bytes`: when it would, it becomes
    <path>.1 (and .1 becomes .2, ...), keeping `keep` old files. The live file keeps its name,
    so a reader that holds a byte offset sees the file get shorter when it rotates; the bytes
    it had not read yet are at the end of <path>.1."""

    def __init__(self, path, max_bytes=32 << 20, keep=4):
        self.path, self.max_bytes, self.keep = path, max_bytes, keep
        self.f = open(path, "ab", buffering=0)

    def write(self, data):
        if self.max_bytes and self.f.tell() + len(data) > self.max_bytes:
            self.rotate()
        self.f.write(data)

    def rotate(self):
        self.f.close()
        for i in range(self.keep - 1, 0, -1):
            if os.path.exists(f"{self.path}.{i}"):
                os.replace(f"{self.path}.{i}", f"{self.path}.{i + 1}")
        if self.keep > 0:
            os.replace(self.path, f"{self.path}.1")
        else:
            os.remove(self.path)
        self.f = open(self.path, "ab", buffering=0)


class Console:
    """The serial port, its log, and reading a command to completion."""

    def __init__(self, board, opener, log_path, log_max_bytes=32 << 20, log_keep=4, modem_rest="asserted"):
        self.board, self.opener, self.log_path = board, opener, log_path
        self.modem_rest = modem_rest
        self.ser = None
        self.port = None
        self.raw_log = RotatingLog(log_path, log_max_bytes, log_keep)
        self.log = ScrubbedLog(self.raw_log)
        self.released_until = 0.0
        self.next_open = 0.0
        self.backoff = 0.5
        self.opens = 0
        self.last_error = None
        self.lock = None
        # Attending: (dialect, until). The board's lines are matched against the dialect's
        # prompts until one is answered or the time passes.
        self.attend = None
        self.attend_tail = ""
        self.attended = 0

    # -- port lifecycle ---------------------------------------------------------------
    def is_open(self):
        return self.ser is not None

    def open(self):
        # An opener for a real board also hands back the device lock it took for the port.
        self.port, self.ser, *held = self.opener()
        self.lock = held[0] if held else None
        self.opens += 1
        # whether this console answers the clearing blank line: learnt again for what now runs
        self.blank_answers = None
        self.backoff = 0.5
        self.last_error = None
        self._mark(f"opened {self.port}")

    def ensure_open(self, wait):
        """Open the port now for a command that has come, whatever the backoff says: a board
        that was away a long time (minutes in its ROM bootloader, say) has pushed the next try
        up to 30 s out, and comes back seconds before the command. Tried for `wait` seconds;
        a port released for a flash is never opened here."""
        end = time.monotonic() + wait
        while self.ser is None:
            if time.monotonic() < self.released_until:
                raise PortError("port closed (released)")
            try:
                self.open()
            except Exception as e:  # noqa: BLE001 - absent board, refused guard, busy port
                self.last_error = str(e)
                if time.monotonic() >= end:
                    raise PortError(f"port closed ({self.last_error})") from e
                time.sleep(0.25)

    def close(self, why):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:  # noqa: BLE001 - a port that is already gone
                pass
            self.ser = None
            self._mark(f"closed: {why}")
        if self.lock is not None:       # the port is free for whoever asked for it
            self.lock.release()
            self.lock = None

    def tick(self):
        """Reopen when due, and let a held partial line out after a second. Called from the
        main loop."""
        self.log.flush(older_than=1.0)
        now = time.monotonic()
        if self.ser is None and now >= self.released_until and now >= self.next_open:
            try:
                self.open()
            except Exception as e:  # noqa: BLE001 - absent board, refused guard, busy port
                self.last_error = str(e)
                self.next_open = now + self.backoff
                self.backoff = min(self.backoff * 2, 30.0)

    def _mark(self, text):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.log.flush()
        self.log.write(f"\n### rxrbench {stamp} {self.board}: {text}\n".encode())

    # -- I/O --------------------------------------------------------------------------
    def read(self):
        """Whatever the board has said since the last call; logged before it is returned."""
        if self.ser is None:
            return b""
        try:
            data = self.ser.read(4096)
        except Exception as e:  # noqa: BLE001 - the port went away mid-read
            self.close(f"read failed: {e}")
            self.next_open = time.monotonic() + self.backoff
            return b""
        if data:
            self.log.write(data)
            if self.attend:
                self._attend(data)
        return data

    def _attend(self, data):
        """Answer a prompt the board printed, once, while a client asked for it."""
        dialect, until, answer = self.attend
        if time.monotonic() >= until:
            self.attend = None
            return
        text = self.attend_tail + strip_ansi(data.decode(errors="replace"))
        *lines, self.attend_tail = text.split("\n")
        for line in lines:
            for pattern, send in dialect.prompts:
                if pattern.search(line):
                    send = answer or send
                    self.attend = None
                    self.attended += 1
                    self._mark(f"answered `{line.strip()[:60]}` with `{send}`")
                    self.write((send + dialect.eol).encode())
                    return

    def reset(self):
        """Pulse the modem lines as a flashing tool's hard reset does: DTR dropped with RTS
        asserted holds the chip's EN low, on the usual auto-reset circuit and on a bridge whose
        RTS drives EN directly (a power-on reset, which a bootloader can tell from a software
        one); the lines then go back to the board's rest state (`MODEM_RESTS`), which releases
        EN either way."""
        if self.ser is None:
            raise PortError("port closed")
        try:
            self.ser.dtr = False
            self.ser.rts = True
            time.sleep(0.1)
            rest_modem_lines(self.ser, self.modem_rest)
            time.sleep(0.05)
        except (OSError, ValueError) as e:
            raise PortError(f"the port does not drive its modem lines: {e}") from e
        self._mark(f"reset by the modem lines (DTR/RTS), left {self.modem_rest}")

    def write(self, data):
        try:
            self.ser.write(data)
            self.ser.flush()
        except Exception as e:  # noqa: BLE001
            self.close(f"write failed: {e}")
            raise PortError(f"port closed: {e}") from e

    def _read_text_until(self, done, end, buf):
        """Read into buf (a list of str) until done(text) is truthy or `end` passes."""
        while True:
            data = self.read()
            if data:
                buf.append(strip_ansi(data.decode(errors="replace")))
                hit = done("".join(buf))
                if hit:
                    return hit
            elif self.ser is None:
                raise PortError("port closed")
            if time.monotonic() >= end:
                return None
            if not data:
                time.sleep(0.01)

    def exec_line(self, cmd, dialect, deadline=8.0, echo_timeout=3.0, expect_reply=True):
        """One command on a console that answers with one line. Read to that line, not to a
        marker: an unknown command is answered too, and a blank line not at all. With
        expect_reply False (a reboot, a wipe) the port closing is an answer."""
        if self.ser is None:
            self.ensure_open(min(deadline, OPEN_WAIT))
        if not cmd or "\n" in cmd or "\r" in cmd:
            raise ValueError("a command is one non-empty line")
        eol = dialect.eol.encode()
        # A blank line clears a half-typed one. Most consoles do not answer it; one that does
        # (`blank_reply`) is waited for, so its answer cannot be read as the command's.
        # Whether it does is learnt once per opening of the port (the same dialect serves
        # firmware that answers it and firmware that does not): the first command waits up
        # to 1 s for that answer, later ones only when it came.
        self.write(eol)
        answers = getattr(self, "blank_answers", None)
        if dialect.blank_reply is not None and answers is not False:
            seen = self._read_text_until(lambda t: dialect.blank_reply.search(t),
                                         time.monotonic() + 1.0, [])
            if answers is None:
                self.blank_answers = bool(seen)
        else:
            self._read_text_until(lambda t: None, time.monotonic() + 0.15, [])
        t0 = time.monotonic()   # the command's deadline runs from here, not from the clearing
        visible = next((cmd[:len(w)] for w in dialect.secret_prefixes if cmd.startswith(w)), cmd)
        buf, raw, sent = [], cmd.encode(), 0
        # A console that echoes nothing is written in paced pieces and read for its answer
        # alone; one that echoes is paced by its echo.
        shown = len(visible.encode()) if dialect.echo else 0
        while sent < len(raw):
            if sent >= shown:
                # Past a secret command's word nothing is echoed: pace the rest instead of
                # waiting for an echo that never comes.
                time.sleep(0.05)
            self.write(raw[sent:sent + CHUNK])
            first, sent = sent, min(sent + CHUNK, len(raw))
            if first >= shown:
                continue
            want = raw[:min(sent, shown)].decode(errors="replace")
            if not self._read_text_until(lambda t, w=want: w in squash(t)[0],
                                         time.monotonic() + echo_timeout, buf):
                return {"ok": False, "error": "no echo", "output": "",
                        "elapsed": time.monotonic() - t0}
        self.write(eol)
        try:
            hit = self._read_text_until(lambda t: dialect.reply.search(t), t0 + deadline, buf)
        except PortError:
            if not expect_reply:
                return {"ok": True, "output": "".join(buf), "elapsed": time.monotonic() - t0}
            raise
        if not hit:
            if not expect_reply:
                # what the board printed meanwhile: a console that stays open across a reset
                # (an ESP32's USB-Serial-JTAG) shows its boot banner here, and nowhere else
                return {"ok": True, "output": "".join(buf), "elapsed": time.monotonic() - t0}
            return {"ok": False, "error": "timeout", "output": "", "elapsed": time.monotonic() - t0}
        return {"ok": True, "output": next(g for g in hit.groups() if g is not None),
                "elapsed": time.monotonic() - t0}

    def exec(self, cmd, deadline=8.0, echo_timeout=3.0):
        if self.ser is None:
            self.ensure_open(min(deadline, OPEN_WAIT))
        if not cmd or "\n" in cmd or "\r" in cmd:
            raise ValueError("a command is one non-empty line")
        t0 = time.monotonic()
        # A clean line to type on: end whatever half-line is there, and drop what it prints.
        self.write(b"\r")
        self._read_text_until(lambda t: None, time.monotonic() + 0.15, [])
        buf = []
        sent = 0
        raw = cmd.encode()
        while sent < len(raw):
            self.write(raw[sent:sent + CHUNK])
            sent = min(sent + CHUNK, len(raw))
            want = raw[:sent].decode(errors="replace")
            if not self._read_text_until(lambda t, w=want: w in squash(t)[0],
                                         time.monotonic() + echo_timeout, buf):
                return {"ok": False, "error": "no echo", "output": "".join(buf),
                        "elapsed": time.monotonic() - t0}
        marker = "mb" + secrets.token_hex(3)
        self.write(b"\r" + marker.encode() + b"\r")
        end_token = marker + NOT_FOUND
        hit = self._read_text_until(lambda t: end_token in t, t0 + deadline, buf)
        text = "".join(buf)
        sq, idx = squash(text)
        start = idx[sq.find(cmd) + len(cmd) - 1] + 1
        if not hit:
            return {"ok": False, "error": "timeout", "output": text[start:].strip("\r\n"),
                    "elapsed": time.monotonic() - t0}
        # The marker was echoed on its own prompt line before the shell complained about it:
        # the output ends where that line starts.
        stop = text.rfind(marker, start, text.find(end_token, start))
        line_start = text.rfind("\n", start, stop) + 1 if stop != -1 else text.find(end_token)
        out = text[start:max(line_start, start)]
        return {"ok": True, "output": out.replace("\r", "").strip("\n"),
                "elapsed": time.monotonic() - t0}


def devreg_opener(board, devreg, baud=115200, modem_rest="asserted"):
    """Resolve the board by name and guard the path, every time the port is opened; then take
    the port's device lock (devlock, deployed beside devreg) and open it exclusively. The lock
    says who has the port to every tool that asks devlock; the exclusive open refuses a tool
    that does not ask anyone. Both go when the port is closed (a release, a stop)."""
    import serial
    sys.path.insert(0, os.path.dirname(os.path.abspath(devreg)))

    def opener():
        try:
            from devlock import DevLock  # pylint: disable=import-error,import-outside-toplevel
        except ImportError as e:
            raise PortError(f"no devlock beside {devreg}: {e}") from e
        r = subprocess.run([sys.executable, devreg, "resolve", board],
                           capture_output=True, text=True, timeout=20, check=False)
        port = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        if r.returncode != 0 or not port:
            raise PortError(f"devreg resolve {board}: rc {r.returncode} {r.stderr.strip()[-200:]}")
        g = subprocess.run([sys.executable, devreg, "guard", board, port],
                           capture_output=True, text=True, timeout=20, check=False)
        if g.returncode != 0:
            raise PortError(f"devreg guard {board} {port}: rc {g.returncode} "
                            f"{(g.stdout + g.stderr).strip()[-200:]}")
        lock = DevLock(port, tool="rxrbench-broker", device=board, expect_long_hold=True)
        if not lock.acquire():
            why = lock.holder_summary()
            lock.release()
            raise PortError(f"{port} is in use: {why}")
        try:
            ser = serial.Serial()
            ser.port, ser.baudrate, ser.timeout = port, baud, 0
            ser.write_timeout = WRITE_TIMEOUT
            ser.exclusive = True
            rest_modem_lines(ser, modem_rest)   # from the open on: a terminal at rest, as the board has it
            ser.open()
        except Exception:
            lock.release()
            raise
        return port, ser, lock
    return opener


def path_opener(path, baud=115200, modem_rest="asserted"):
    """A path given directly: tests and the simulated board only. Real boards go through devreg.

    `path` may be a callable: a simulated board gets a new pty at every boot, as a real one
    may get a new ttyACM, so the path is asked for at every open.
    """
    import serial

    def opener():
        port = path() if callable(path) else path
        if not port:
            raise PortError("the board has no console yet")
        ser = serial.Serial()
        ser.port, ser.baudrate, ser.timeout = port, baud, 0
        ser.write_timeout = WRITE_TIMEOUT
        if modem_rest != "asserted":      # a pty (the simulated boards) has no lines to drive
            rest_modem_lines(ser, modem_rest)
        ser.open()
        return port, ser
    return opener


class Broker:
    def __init__(self, console, sock_path):
        self.console, self.sock_path = console, sock_path
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(sock_path)
        os.chmod(sock_path, 0o600)
        self.listener.listen(8)
        self.listener.setblocking(False)
        self.client = None
        self.inbuf = b""
        self.stop = False
        self.started = time.time()
        self.execs = 0

    def handle(self, req):
        op = req.get("op")
        c = self.console
        if op == "status":
            return {"ok": True, "board": c.board, "port": c.port, "open": c.is_open(),
                    "opens": c.opens, "execs": self.execs, "last_error": c.last_error,
                    "released_for": max(0.0, c.released_until - time.monotonic()),
                    "uptime": time.time() - self.started, "log": c.log_path}
        if op == "exec":
            self.execs += 1
            # The broker serves one thing at a time: a client's deadline is how long everyone
            # else waits, so it is bounded here, whatever the client asks.
            deadline = min(max(float(req.get("deadline", 8.0)), 0.1), MAX_DEADLINE)
            cmd, name = str(req.get("cmd", "")), req.get("dialect")
            dialect = None if name in MARKER else DIALECTS.get(name)
            if name not in MARKER and dialect is None:
                return {"ok": False, "error": f"unknown dialect {name!r} (known: "
                                              f"{', '.join(sorted(DIALECTS)) or 'none'})"}
            # The secret the request names; a command the dialect knows to carry one names
            # it by its shape, for a client that did not.
            secret = str(req.get("secret") or "") or (dialect.secret_of(cmd) if dialect else "")
            c.log.hide(secret)
            reply = bool(req.get("expect_reply", True))
            if dialect:
                r = c.exec_line(cmd, dialect, deadline, expect_reply=reply)
            else:
                r = c.exec(cmd, deadline)
            if secret:
                for k in ("output", "error"):
                    if isinstance(r.get(k), str):
                        r[k] = r[k].replace(secret, REDACTED)
            return r
        if op == "release":
            ttl = min(max(float(req.get("ttl", 120.0)), 1.0), 900.0)
            port = c.port
            c.close(f"released for {ttl:.0f} s")
            c.released_until = time.monotonic() + ttl
            return {"ok": True, "port": port, "ttl": ttl}
        if op == "mark":
            text = str(req.get("text", "")).strip()
            if not text or "\n" in text or "\r" in text or len(text) > 400:
                return {"ok": False, "error": "a mark is one line of 1-400 characters"}
            c._mark(text)
            return {"ok": True}
        if op == "attend":
            ttl = min(max(float(req.get("ttl", 30.0)), 0.0), 300.0)
            name = req.get("dialect")
            dialect = DIALECTS.get(name)
            if ttl == 0:
                c.attend = None
                return {"ok": True, "attending": False, "answered": c.attended}
            if dialect is None or not dialect.prompts:
                return {"ok": False, "error": f"dialect {name!r} has no prompts to answer"}
            answer = str(req.get("answer") or "").strip() or None
            if answer and ("\n" in answer or "\r" in answer):
                return {"ok": False, "error": "an answer is one line"}
            c.attend, c.attend_tail = (dialect, time.monotonic() + ttl, answer), ""
            return {"ok": True, "attending": True, "ttl": ttl, "answered": c.attended}
        if op == "reset":
            c.reset()
            return {"ok": True, "port": c.port}
        if op == "reacquire":
            # afresh: a port that was open all along is closed and opened again, so an opener
            # that starts a simulated board finds one that died (a reset) and starts it
            c.released_until = 0.0
            c.next_open = 0.0
            if c.is_open():
                c.close("reacquired")
            c.tick()
            return {"ok": c.is_open(), "port": c.port, "error": c.last_error}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def serve_client(self):
        try:
            data = self.client.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self.client.close()
            self.client = None
            self.inbuf = b""
            return
        self.inbuf += data
        while b"\n" in self.inbuf:
            line, self.inbuf = self.inbuf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                resp = self.handle(json.loads(line))
            except (PortError, ValueError) as e:
                resp = {"ok": False, "error": str(e)}
            self.client.setblocking(True)
            try:
                self.client.sendall(json.dumps(resp).encode() + b"\n")
            except OSError:
                pass
            self.client.setblocking(False)

    def run(self):
        while not self.stop:
            self.console.tick()
            fds = [self.listener] + ([self.client] if self.client else [])
            if self.console.is_open():
                fds.append(self.console.ser)
            sel = selectors.DefaultSelector()
            for f in fds:
                try:
                    sel.register(f, selectors.EVENT_READ)
                except (OSError, ValueError):
                    # closed between the check and here (a stop from a signal or another
                    # thread; pyserial's PortNotOpenError is an OSError): next turn decides
                    pass
            ready = [k.fileobj for k, _ in sel.select(timeout=0.25)]
            sel.close()
            if self.console.is_open() and self.console.ser in ready:
                self.console.read()
            if self.client and self.client in ready:
                self.serve_client()
            if self.listener in ready:
                conn, _ = self.listener.accept()
                if self.client is None:
                    conn.setblocking(False)
                    self.client = conn
                else:  # one client at a time: a second one is told so, not queued silently
                    conn.sendall(b'{"ok": false, "error": "busy"}\n')
                    conn.close()

    def shutdown(self):
        self.stop = True
        try:
            self.listener.close()
            os.unlink(self.sock_path)
        except OSError:
            pass
        self.console.close("broker stopped")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--board", required=True)
    ap.add_argument("--socket", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--log-max-mb", type=float, default=32.0,
                    help="rotate the log at this size (0: never)")
    ap.add_argument("--log-keep", type=int, default=4, help="rotated logs kept (<log>.1 ...)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--devreg", help="path to devreg.py: resolve and guard the board by name")
    src.add_argument("--port", help="a fixed path (tests and the simulated board only)")
    ap.add_argument("--dialects", action="append", default=[],
                    help="a directory of console rules, one JSON file each (load_dialects); "
                         "the dialects.d beside this file is always read")
    ap.add_argument("--modem-rest", choices=MODEM_RESTS,
                    help="where the port's DTR and RTS rest while the broker holds it and after a "
                         "reset (default: $RXRBENCH_MODEM_REST, else asserted)")
    a = ap.parse_args(argv)
    rest = a.modem_rest or os.environ.get("RXRBENCH_MODEM_REST") or "asserted"
    if rest not in MODEM_RESTS:
        ap.error(f"RXRBENCH_MODEM_REST={rest!r}: one of {', '.join(MODEM_RESTS)}")
    for d in a.dialects:
        load_dialects(d)
    opener = (devreg_opener(a.board, a.devreg, modem_rest=rest) if a.devreg
              else path_opener(a.port, modem_rest=rest))
    os.makedirs(os.path.dirname(os.path.abspath(a.log)), exist_ok=True)
    broker = Broker(Console(a.board, opener, a.log, int(a.log_max_mb * (1 << 20)), a.log_keep,
                            modem_rest=rest), a.socket)

    # systemd stops a unit with SIGTERM. Leave the loop and clean up: a socket file left
    # behind says "a broker owns this board" to everything that checks for one.
    def stop(_signum, _frame):
        broker.stop = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        broker.run()
    finally:
        broker.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
