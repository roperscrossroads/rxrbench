"""A Linux device's ssh host key: what it holds, and one put back.

The key pair is two files on the device (`<path>` and `<path>.pub`). Read, it is one value
the site's store keeps: both files' text as JSON, named by the fingerprint ssh shows for the
public key (computed here, so the device needs no ssh-keygen). Put back, the files are
replaced whole. An ssh server that reads its key for each connection (OpenSSH does) presents
the new one at once; one that read it at start keeps the old one until it restarts.
"""
import base64
import hashlib
import json
import shlex


class HostKeyError(Exception):
    pass


def fingerprint(public):
    """`SHA256:...` of a public key line, as `ssh-keygen -l` prints it."""
    parts = public.split()
    if len(parts) < 2:
        raise HostKeyError("the public key file is not `<type> <base64> [comment]`")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError as e:
        raise HostKeyError("the public key does not decode") from e
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def pack(private, public):
    return json.dumps({"private": private, "public": public}, sort_keys=True)


def unpack(value):
    try:
        d = json.loads(value)
        return d["private"], d["public"]
    except (ValueError, KeyError, TypeError) as e:
        raise HostKeyError("the escrowed host key is not a key pair this tool wrote") from e


def find(target, paths):
    """The first of `paths` that is a private key file on the device."""
    for path in paths:
        if target.run(f"test -f {shlex.quote(path)}", timeout=20)[0] == 0:
            return path
    raise HostKeyError("no private host key on the device at " + " or ".join(paths))


def read(target, path):
    """(fingerprint, value) of the device's key pair at `path`."""
    q = shlex.quote(path)
    rc, private = target.run(f"cat {q}", timeout=20)
    if rc != 0 or "PRIVATE KEY" not in private:
        raise HostKeyError(f"no private host key at {path} on the device")
    rc, public = target.run(f"cat {q}.pub", timeout=20)
    if rc != 0:
        raise HostKeyError(f"no public host key at {path}.pub on the device")
    return fingerprint(public), pack(private, public.strip() + "\n")


def write(target, path, value):
    """Replace the device's key pair with the escrowed one. Each file is written beside its
    place with the mode it must have, then moved over it, so a cut connection leaves either
    the old key or the new one. The private key travels on the connection, never as an
    argument or a file on this host. The public key goes first: the moment the private key
    is in place, the server may present it, and this connection's pin no longer holds."""
    private, public = unpack(value)
    target.write(path + ".pub", public, mode="644")
    target.write(path, private, mode="600")


def known_hosts_line(host, port, public):
    """The line that pins a device's host key, as ssh writes it in a known_hosts file."""
    kind, blob = public.split()[:2]
    name = host if int(port) == 22 else f"[{host}]:{port}"
    return f"{name} {kind} {blob}"
