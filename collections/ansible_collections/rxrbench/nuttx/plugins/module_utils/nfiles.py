"""NSH through a board's broker: the storage mounted, one file read, one written. Runs on the
bench host (a module's helper)."""
import os

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client

# `echo <value> > <path>`: the value is the secret when the file is one
broker_client.secret_shapes(payloads=[r"^(echo )(\S+)( > \S+)$"])

REDACTED = "<secret>"


class FilesError(Exception):
    pass


def mounted(board, at, deadline=8.0):
    out = board.exec("mount", deadline=deadline)
    return any(ln.strip().startswith(at + " ") for ln in out.splitlines())


def mount(board, storage, deadline=8.0):
    """The board's storage mounted where it declares it, unless it is. hostfs (the sim): the
    host directory is made here, where the sim runs."""
    at, kind = storage["at"], storage.get("type", "hostfs")
    if mounted(board, at, deadline):
        return False
    if kind == "hostfs":
        os.makedirs(storage["source"], exist_ok=True)
        cmd = f"mount -t hostfs -o fs={storage['source']} {at}"
    else:
        cmd = f"mount -t {kind} {storage['source']} {at}"
    out = board.exec(f"mkdir {at}", deadline=deadline)
    out = board.exec(cmd, deadline=deadline)
    if out.strip() or not mounted(board, at, deadline):
        raise FilesError(f"{at}: not mounted ({out.strip().splitlines()[0] if out.strip() else 'no error, not there'})")
    return True


def read(board, path, deadline=8.0, secret=None):
    """The file's content as one line, None when there is no such file."""
    out = board.exec(f"cat {path}", deadline=deadline, secret=secret)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        return ""
    if lines[0].startswith("nsh: cat:") or "No such file" in lines[0]:
        return None
    if secret and lines[0] == REDACTED:
        return secret
    return lines[0]


def write(board, path, value, secret=False, deadline=8.0):
    if value is None or value.split() != [value]:
        raise FilesError(f"{path}: a value is one word (no whitespace, not empty)")
    out = board.exec(f"echo {value} > {path}", deadline=deadline, secret=value if secret else None)
    if out.strip():
        raise FilesError(f"{path}: the shell said {out.strip().splitlines()[0]!r}")
    if read(board, path, deadline, secret=value if secret else None) != value:
        raise FilesError(f"{path}: written, but reads back as something else")
