"""Zephyr's settings shell through a board's broker: one setting read, one written, as
strings. Runs on the bench host (a module's helper)."""
import hashlib
import re

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, stateline

# `settings write string <name> <value>`: the value is the secret when the setting is one;
# an error quotes the command by its words and the value's length, never the value
broker_client.secret_shapes(payloads=[r"^(settings write (?:string|hex) \S+ )(\S+)()$"])

NOT_FOUND = "Setting not found"
REDACTED = "<secret>"       # what the broker answers in place of a secret it was named
# The settings shell's `settings read` prints at most this much of a value (Zephyr's
# SETTINGS_MAX_VAL_LEN, a fixed #define, the same from 4.4 to 4.5), then this line. Writing and
# storage are not limited by it; reading back through the shell is. A value the shell cannot
# show whole cannot be checked, so it is refused: never written, never taken as read.
MAX_SHOWN = 256
TRUNCATED = "(The output has been truncated)"


def too_long(name, value, digest_command=None):
    """Why `value` cannot be kept in `name` through the settings shell, or None. A board whose
    firmware prints a setting's digest (`digest_command`) is checked by it, at any length."""
    if digest_command:
        return None
    if value is not None and len(value) > MAX_SHOWN:
        return (f"{name}: {len(value)} bytes; Zephyr's settings shell reads back at most "
                f"{MAX_SHOWN}, so it could never be checked (refused, nothing written)")
    return None


class SettingsError(Exception):
    pass


class Truncated(SettingsError):
    """The board holds more of a setting than its shell can show: its value is unknown, except
    that it is longer than MAX_SHOWN (so it is not any value the shell could check)."""


READ_ASKS = 3


def read(board, name, deadline=8.0, secret=None):
    """The setting's value as a string, or None when the board has no such setting. The
    broker's marker read gives the lines between the command's echo and the marker. `secret`
    is the value expected of a secret setting: the broker keeps it out of its log when the
    board prints it (a value nobody expected is the dialect file's scrub rules' to catch)."""
    # A secret longer than the shell shows is printed only to MAX_SHOWN: that part is what the
    # broker must keep out of its log (the read then fails, below).
    shown = secret[:MAX_SHOWN] if secret and len(secret) > MAX_SHOWN else secret
    # The shell prints a value on one line. More than one means the board's own console message
    # landed in the reply (a just-booted board's WiFi join, seen on a Pico W, where the message
    # was then taken for the SSID and the plan reported drift). The command only reads, so it is
    # asked again; a reply that never comes back whole is an error, not a guess.
    for _ in range(READ_ASKS):
        out = board.exec(f"settings read string {name}", deadline=deadline, secret=shown)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        cut = any(ln.startswith(TRUNCATED) for ln in lines)
        lines = [ln for ln in lines if not ln.startswith(TRUNCATED)]
        if len(lines) <= 1:
            break
    else:
        raise SettingsError(f"{name}: `settings read string` never answered with one line")
    if not lines:
        return ""
    if lines[0].startswith(NOT_FOUND):
        return None
    if cut:
        raise Truncated(f"{name}: the board holds more than the {MAX_SHOWN} bytes its settings "
                        "shell can show: what it holds cannot be read")
    # the broker took the named secret out of its answer: the board printed exactly it
    return shown if secret and lines[0] == REDACTED else lines[0]


DIGEST_ASKS = 3
DIGEST_LINE = re.compile(r"^digest (\S+) (?:(none)|(\d+) ([0-9a-f]{64}))\s*$")


def digest(board, name, command, deadline=8.0):
    """(length, SHA-256 hex) of the board's `name`, from the firmware's own `<command> <name>`
    ("digest <name> <length> <sha256>", or "digest <name> none"): None when it holds no such
    setting. The value itself never crosses the console."""
    # The command only reads, so it is asked again when no digest line comes back: a board
    # that has just booted prints its WiFi join ("Connected", "zapp 3.0: up") on the console
    # whenever it likes, and a message that lands inside the digest line splits it (seen on
    # a Pico W). A board without the command still fails, after the last ask.
    for _ in range(DIGEST_ASKS):
        out = board.exec(f"{command} {name}", deadline=deadline)
        for line in out.splitlines():
            m = DIGEST_LINE.match(line.strip())
            if m and m.group(1) == name:
                return None if m.group(2) else (int(m.group(3)), m.group(4))
    raise SettingsError(f"{name}: `{command} {name}` printed no digest line")


def digest_of(value):
    data = value.encode()
    return len(data), hashlib.sha256(data).hexdigest()


def differs(board, name, want, deadline=8.0, secret=None, digest_command=None):
    """Does the board's `name` differ from `want`? Through the firmware's digest when it has one
    (`digest_command`), for a secret (it stays off the console) and for a value the shell could
    not show whole; else read through the shell. A value the shell cuts short is longer than any
    `want` it can show: it differs, though it cannot be read."""
    if digest_command and (secret is not None or len(want) > MAX_SHOWN):
        return digest(board, name, digest_command, deadline) != digest_of(want)
    try:
        return read(board, name, deadline=deadline, secret=secret) != want
    except Truncated:
        return True


def write(board, name, value, secret=False, deadline=8.0, digest_command=None):
    """Write one setting and check it (read back, or by the firmware's digest); the value never
    reaches the broker's log when it is a secret. A value with whitespace in it is refused: the
    shell would split it."""
    if value is None or value.split() != [value]:
        raise SettingsError(f"{name}: a setting is one word (no whitespace, not empty)")
    if too_long(name, value, digest_command):
        raise SettingsError(too_long(name, value, digest_command))
    out = board.exec(f"settings write string {name} {value}", deadline=deadline,
                     secret=value if secret else None)
    if out.strip():
        raise SettingsError(f"{name}: the shell said {out.strip().splitlines()[0]!r}")
    if differs(board, name, value, deadline=deadline, secret=value if secret else None,
               digest_command=digest_command):
        raise SettingsError(f"{name}: written, but reads back as something else")


def fingerprint(value):
    """A short, safe name for a secret's value (a quarter of its hash)."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def state(board, command, deadline=8.0):
    """The board's state line (docs/PLUGINS.md), asked with `command` (zapp's `zapp state`) and
    checked by core's parser: its JSON. An answer with no state line, or a damaged one, is a
    SettingsError: a board whose layout names the command is expected to print one."""
    out = board.exec(command, deadline=deadline)
    try:
        found = stateline.parse_line(out)
    except stateline.StateError as e:
        raise SettingsError(f"{board.board}: `{command}`: {e}") from e
    if found is None:
        raise SettingsError(f"{board.board}: `{command}` printed no state line")
    return found
