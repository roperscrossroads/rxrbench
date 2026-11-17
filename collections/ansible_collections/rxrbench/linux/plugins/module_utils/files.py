"""Declared files on a Linux device: small text files the layout holds whole (a client's
config on the data partition, a drop-in), compared by digest and mode and written over ssh.

Nothing is read back by value: what the device holds is asked as a SHA-256, so a file with a
secret in it never crosses the connection towards the bench."""
import hashlib
import posixpath
import re
import shlex

from ansible_collections.rxrbench.core.plugins.module_utils import ssh

HELD = re.compile(r"^held (\S+)(?: (\S+))?\s*$", re.M)


def digest_of(content):
    return hashlib.sha256(content.encode()).hexdigest()


def held(target, path):
    """(sha256, mode) of the file on the device, mode None when its `stat` cannot say;
    (None, None) when there is no such file."""
    q = shlex.quote(path)
    rc, out = target.run(
        f"if [ -f {q} ]; then echo held $(sha256sum < {q} | cut -d' ' -f1) "
        f"$(stat -c %a {q} 2>/dev/null); else echo held none; fi", timeout=20)
    m = HELD.search(out)
    if rc != 0 or not m:
        raise ssh.SshError(f"{path}: the device did not say what it holds (rc {rc}: "
                           f"{out.strip()[-200:]})")
    if m.group(1) == "none":
        return None, None
    return m.group(1), (m.group(2) or None)


def differs(target, path, content, mode):
    """Why the file is not as declared, in a word, or None."""
    sha, has_mode = held(target, path)
    if sha is None:
        return "missing"
    if sha != digest_of(content):
        return "content"
    if has_mode is not None and has_mode.lstrip("0") != str(mode).lstrip("0"):
        return f"mode {has_mode}"
    return None


def write(target, path, content, mode):
    """The file, whole, with its mode; its directory made when it is not there."""
    target.check(f"mkdir -p {shlex.quote(posixpath.dirname(path) or '/')}", timeout=20)
    target.write(path, content, mode=str(mode))
