"""The last full read of a board, and when it may stand in for asking the board
(docs/ARCHITECTURE.md#checking-depth).

Both records live on the board's bench host, under its state directory (RXRBENCH_HOST_STATE,
default ~/rxrbench/state):

    last-read/<board>.json   what drive() last read in full: {v, board, flasher, family, t, at,
                             identity, busid, serial}
    usb-present.json         the plug watcher's snapshot: its pid and watching_since
    plug-events.jsonl(.1)    the plug watcher's log: a start line, then each USB add or remove

    rec, why = lastread.stand_in(board, flasher, family, busid)
    # rec: the recorded identity, or None; why: the reason either way, for `identity_checked`

Nothing here asks a device. A record that cannot be read, or a watcher that cannot be shown to
have watched without a break, means the board is asked.
"""
import calendar
import json
import os
import time

VERSION = 1


def state_dir():
    return os.path.expanduser(os.environ.get("RXRBENCH_HOST_STATE", "~/rxrbench/state"))


def _path(board, root=None):
    return os.path.join(root or state_dir(), "last-read", f"{board}.json")


def _json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _epoch(stamp):
    """`2026-11-08T07:10:18Z` as seconds, or None."""
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def serial_at(busid, root=None):
    """The USB serial the watcher's snapshot shows on `busid`, or None."""
    snap = _json(os.path.join(root or state_dir(), "usb-present.json")) or {}
    for d in snap.get("devices") or []:
        if d.get("busid") == busid:
            return d.get("serial")
    return None


def record(board, flasher, family, ident, busid, now=None, root=None):
    """Keep `ident` as the board's last full read. Only on a bench host (its state directory
    exists) and for a board on a USB port; anything else keeps no record."""
    root = root or state_dir()
    if not board or not busid or not os.path.isdir(root):
        return None
    now = time.time() if now is None else now
    rec = {"v": VERSION, "board": board, "flasher": flasher, "family": family, "t": now,
           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
           "identity": ident, "busid": busid, "serial": serial_at(busid, root)}
    path = _path(board, root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(rec, f)
        os.replace(path + ".tmp", path)
    except OSError:
        return None
    return rec


def forget(board, root=None):
    """No last full read for the board: before a write, and when a read fails or finds other
    firmware."""
    if not board:
        return
    try:
        os.remove(_path(board, root))
    except OSError:
        pass


def _events(root):
    out = []
    for name in ("plug-events.jsonl.1", "plug-events.jsonl"):
        try:
            with open(os.path.join(root, name)) as f:
                for ln in f:
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        continue
        except OSError:
            continue
    return out


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            return b"plugwatch" in f.read()
    except OSError:
        return True     # no /proc to ask: the pid alone


def stand_in(board, flasher, family, busid, root=None, alive=None):
    """(identity, reason): the board's last full read when the plug watcher's record shows it
    cannot have changed through USB since; (None, why not) otherwise."""
    root = root or state_dir()
    rec = _json(_path(board, root)) if board else None
    if not rec or rec.get("v") != VERSION or not isinstance(rec.get("identity"), dict):
        return None, "no last full read of the board on its bench host"
    if rec.get("flasher") != flasher or rec.get("family") != family:
        return None, (f"the last full read was through {rec.get('flasher')} as "
                      f"{rec.get('family')}, not {flasher} as {family}")
    t = float(rec.get("t") or 0)
    snap = _json(os.path.join(root, "usb-present.json")) or {}
    since = _epoch(snap.get("watching_since"))
    if since is None or not snap.get("pid"):
        return None, ("the plug watcher on the bench host keeps no watch record "
                      "(older than this checkout: `just hosts-setup`)")
    if not (alive or _alive)(snap["pid"]):
        return None, "the plug watcher on the bench host is not running"
    if since > t:
        return None, (f"the plug watcher restarted at {snap['watching_since']}, after the last "
                      f"full read ({rec.get('at')}): what happened between is not known")
    events = _events(root)
    stamps = [e for e in (_epoch(e.get("t")) for e in events) if e is not None]
    if not stamps or min(stamps) > t:
        return None, "the plug watcher's log no longer reaches back to the last full read"
    if busid != rec.get("busid"):
        return None, (f"the board is on USB port {busid}, its last full read was on "
                      f"{rec.get('busid')}")
    serials = {s for s in (rec.get("serial"),) if s}
    floor = int(t)
    for e in events:
        at = _epoch(e.get("t"))
        if at is None or at < floor or e.get("action") not in ("add", "remove"):
            continue
        if e.get("busid") == busid or (e.get("serial") and e.get("serial") in serials):
            return None, (f"the plug watcher saw the board's port change at {e.get('t')} "
                          f"({e.get('action')}), after the last full read")
    return rec["identity"], (f"the last full read ({rec.get('at')}); no USB event on its port "
                             "since")
