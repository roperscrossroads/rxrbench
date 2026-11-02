"""A Linux device's ssh host key: read as one value, named by ssh's fingerprint, put back whole."""
import base64
import hashlib

import pytest

from ansible_collections.rxrbench.linux.plugins.module_utils import hostkey

BLOB = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00 " + bytes(range(32))
PUBLIC = "ssh-ed25519 " + base64.b64encode(BLOB).decode() + " root@node\n"
PRIVATE = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"


class Device:
    """The two files on a device, behind the ssh transport's three methods."""
    def __init__(self, files=None):
        self.files = dict(files if files is not None else {"/k": PRIVATE, "/k.pub": PUBLIC})
        self.modes = {}

    def run(self, cmd, timeout=30):
        if cmd.startswith("test -f "):
            return (0, "") if cmd[8:] in self.files else (1, "")
        if cmd.startswith("cat "):
            path = cmd[4:]
            return (0, self.files[path]) if path in self.files else (1, "No such file")
        return 0, ""

    def write(self, remote, text, mode="600", timeout=30):
        self.files[remote], self.modes[remote] = text, mode


def test_the_fingerprint_is_the_one_ssh_shows():
    want = "SHA256:" + base64.b64encode(hashlib.sha256(BLOB).digest()).decode().rstrip("=")
    assert hostkey.fingerprint(PUBLIC) == want
    with pytest.raises(hostkey.HostKeyError):
        hostkey.fingerprint("garbage")


def test_a_key_pair_is_read_as_one_value_and_put_back_whole():
    fp, value = hostkey.read(Device(), "/k")
    assert fp == hostkey.fingerprint(PUBLIC) and hostkey.unpack(value) == (PRIVATE, PUBLIC)
    fresh = Device({"/k": PRIVATE.replace("AAAA", "BBBB"), "/k.pub": PUBLIC.replace("root@node", "x")})
    assert hostkey.read(fresh, "/k")[1] != value
    hostkey.write(fresh, "/k", value)
    assert hostkey.read(fresh, "/k") == (fp, value)
    assert fresh.modes == {"/k": "600", "/k.pub": "644"}


def test_a_device_with_no_key_or_a_store_entry_of_another_shape_is_said():
    with pytest.raises(hostkey.HostKeyError, match="no private host key"):
        hostkey.read(Device({}), "/k")
    with pytest.raises(hostkey.HostKeyError, match="no public host key"):
        hostkey.read(Device({"/k": PRIVATE}), "/k")
    with pytest.raises(hostkey.HostKeyError, match="not a key pair"):
        hostkey.unpack("just-a-string")


def test_a_host_key_is_pinned_the_way_ssh_writes_it():
    blob = PUBLIC.split()[1]
    assert hostkey.known_hosts_line("192.0.2.9", 22, PUBLIC) == f"192.0.2.9 ssh-ed25519 {blob}"
    assert hostkey.known_hosts_line("127.0.0.1", 22201, PUBLIC) == f"[127.0.0.1]:22201 ssh-ed25519 {blob}"


def test_the_key_is_the_first_of_the_familys_places_that_is_there():
    """An image's ssh server makes the type of key it makes: the family lists where each would be."""
    d = Device({"/data/ssh/ssh_host_ecdsa_key": PRIVATE, "/data/ssh/ssh_host_ecdsa_key.pub": PUBLIC})
    paths = ["/data/ssh/ssh_host_ed25519_key", "/data/ssh/ssh_host_ecdsa_key"]
    assert hostkey.find(d, paths) == "/data/ssh/ssh_host_ecdsa_key"
    with pytest.raises(hostkey.HostKeyError, match="ed25519_key or /data/ssh/ssh_host_ecdsa_key"):
        hostkey.find(Device({}), paths)
