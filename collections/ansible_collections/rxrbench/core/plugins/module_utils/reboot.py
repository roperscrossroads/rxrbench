"""Reboot a board through its console, and know that it did.

An answer after the command proves nothing: a board answers for a moment before it goes down,
and a console that stays open across a software reset never goes quiet. So a reboot counts
when the console went down and came back (a command failed, or the broker reopened the port
in between), or when the board's uptime is less than it would be had it kept running. A
family whose uptime cannot be read is rebooted only when it names what proves it otherwise:
its boot banner (`banner`, printed under the command) or the port going away, and a command
that answers once it is back (`ready`); else there would be no telling whether it happened.
The commands are the family's (familyspec.py `reboot`).
"""
import re
import time

from . import broker_client, familyspec, ssh, stateline


class RebootError(Exception):
    pass


def commands(family):
    """How a family's board is rebooted and its uptime read (familyspec.py `reboot`), or
    RebootError saying why it cannot be."""
    c = familyspec.of(family)["reboot"]
    if c is None:
        raise RebootError(f"a {family!r} board has no console this tool can reboot it "
                          f"through{familyspec.unloaded(family)}")
    if c.get("uptime") is None and not c.get("ready"):
        raise RebootError(f"a {family!r} board does not say how long it has been up, so a "
                          "reboot could not be told from none")
    return c


def _send(board, c):
    """The reboot command; what the board printed under it (a console that stays open shows
    its boot banner there), or None when the console closed under it."""
    try:
        if c.get("dialect"):
            return board.exec(c["reboot"], deadline=3.0, dialect=c["dialect"],
                              expect_reply=c.get("expect_reply", True))
        return board.exec(c["reboot"], deadline=3.0)
    except (broker_client.BrokerError, ssh.SshError):
        return None   # the console closing under the command is the reboot


def _reboot_without_uptime(board, c, settle, deadline):
    """A board that cannot say its uptime: down (the port reopened, or a command failed) or its
    banner printed, then its `ready` command answered."""
    start = time.monotonic()
    opened = broker_client.opens(board)
    said = _send(board, c)
    banner = c.get("banner")
    went_down = said is None or bool(banner and said and re.search(banner, said, re.M))
    why = "nothing showed it rebooted: no banner, the console never went down"
    kw = {"dialect": c["dialect"]} if c.get("dialect") else {}
    while time.monotonic() - start < settle:
        try:
            board.exec(c["ready"], deadline=deadline, **kw)
        except (broker_client.BrokerError, ssh.SshError) as e:
            went_down, why = True, str(e)
            time.sleep(0.5)
            continue
        went_down = went_down or broker_client.reopened(board, opened)
        if went_down:
            return {"before_s": None, "after_s": None, "took_s": round(time.monotonic() - start, 1),
                    "seen_by": "banner" if said and banner and re.search(banner, said, re.M)
                    else "the console going down"}
        time.sleep(0.5)
    raise RebootError(f"no reboot seen within {settle:.0f} s: {why}")


def uptime(board, family, deadline=8.0):
    """Seconds since the board booted, as it says."""
    up = commands(family)["uptime"]
    if up.get("command"):
        out = board.exec(up["command"], deadline=deadline)
        m = re.search(up["pattern"], out)
        if not m:
            raise RebootError(f"`{up['command']}` answered {out.strip()[:80]!r}, not an uptime")
        return int(m.group(1)) * up.get("scale", 1)
    spec = familyspec.of(family)["identity"]
    j = stateline.read(board, spec, deadline) if spec else None
    if j is None or j.get(up["state_key"]) is None:
        raise RebootError(f"`{(spec or {}).get('command')}` did not say an uptime")
    return float(j[up["state_key"]])


def reboot(board, family, settle=60.0, deadline=8.0):
    """Reboot and wait until the board answers again, for up to `settle` seconds.
    Returns {before_s, after_s, took_s}."""
    c = commands(family)
    if c.get("uptime") is None:
        return _reboot_without_uptime(board, c, settle, deadline)
    before, start = uptime(board, family, deadline), time.monotonic()
    opened = broker_client.opens(board)
    try:
        if c.get("dialect"):
            board.exec(c["reboot"], deadline=3.0, dialect=c["dialect"],
                       expect_reply=c.get("expect_reply", True))
        else:
            board.exec(c["reboot"], deadline=3.0)
    except (broker_client.BrokerError, ssh.SshError):
        pass  # the console closing under the command is the reboot
    went_down, why = False, "the board kept running"
    while time.monotonic() - start < settle:
        try:
            up = uptime(board, family, deadline)
        except (broker_client.BrokerError, ssh.SshError, RebootError, stateline.StateError) as e:
            went_down, why = True, str(e)
            time.sleep(0.5)
            continue
        went_down = went_down or broker_client.reopened(board, opened)
        # a margin of 2 s keeps a board that kept running from looking rebooted
        if went_down or up + 2 < before + (time.monotonic() - start):
            return {"before_s": before, "after_s": up,
                    "took_s": round(time.monotonic() - start, 1)}
        time.sleep(0.5)
    raise RebootError(f"no reboot seen within {settle:.0f} s: {why}")
