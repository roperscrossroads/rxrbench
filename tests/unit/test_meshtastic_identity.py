"""The node's identity key: what a restore leaves alone, and reading stock firmware's."""
import base64

import pytest

from ansible_collections.rxrbench.mesh.plugins.module_utils import state
from ansible_collections.rxrbench.mesh.plugins.modules import meshtastic_identity as mi

config_pb2 = pytest.importorskip("meshtastic.protobuf.config_pb2")

OLD, NEW = bytes(range(32)), bytes(range(32, 64))
ADMINS = [b"\xa1" * 32, b"\xa2" * 32]


def security(private=OLD, admins=ADMINS, managed=False):
    sec = config_pb2.Config.SecurityConfig()
    sec.private_key, sec.public_key = private, b"\x09" * 32
    sec.admin_key.extend(admins)
    sec.is_managed, sec.serial_enabled, sec.debug_log_api_enabled = managed, True, True
    return sec


def test_a_restored_key_leaves_the_nodes_admin_keys_and_security_settings_as_they_were():
    """The write sends the node's whole security config back. The firmware stores what it is
    sent (it keeps its own fields only when the message is a bare key and nothing else), so
    what is sent must still carry the admin keys."""
    sec = security()
    mi.set_private_key(sec, NEW)
    assert bytes(sec.private_key) == NEW
    assert bytes(sec.public_key) == b"", "derived by the firmware at boot"
    assert list(sec.admin_key) == ADMINS
    assert (sec.is_managed, sec.serial_enabled, sec.debug_log_api_enabled) == (False, True, True)
    # and it survives the wire: what the node decodes is what was meant
    back = config_pb2.Config.SecurityConfig.FromString(sec.SerializeToString())
    assert list(back.admin_key) == ADMINS and bytes(back.private_key) == NEW


class _Lock:
    held, seen = True, []

    def __init__(self, path):
        _Lock.seen.append(path)

    def acquire(self):
        return _Lock.held

    def release(self):
        pass

    def holder_summary(self):
        return "pid 1 (other)"


class _Node:
    """A stock node as its phone API shows it: one security config, kept across connections."""
    def __init__(self, sec, num=0x0A0B0C0D, takes=True):
        self.sec, self.num, self.takes, self.writes, self.opened = sec, num, takes, 0, 0


class _Iface:
    def __init__(self, node):
        node.opened += 1
        self.node = node
        self.myInfo = type("M", (), {"my_node_num": node.num})()
        sec = config_pb2.Config.SecurityConfig()
        sec.CopyFrom(node.sec)
        self.localNode = type("N", (), {"localConfig": type("C", (), {"security": sec})(),
                                        "writeConfig": self._write})()
        self.closed = False

    def _write(self, name):
        assert name == "security"
        self.node.writes += 1
        if self.node.takes:
            self.node.sec.CopyFrom(self.localNode.localConfig.security)
            self.node.sec.public_key = b"\x0b" * 32    # the firmware derives it

    def close(self):
        self.closed = True


class _Module:
    def __init__(self, params, check_mode=False):
        self.params, self.check_mode = {"apply_allowed": True, **params}, check_mode

    def exit_json(self, **kw):
        raise _Exit(dict(kw, failed=False))

    def fail_json(self, **kw):
        raise _Exit(dict(kw, failed=True))


class _Exit(Exception):
    pass


P = {"board": "b1", "device": "stock1", "transport": "serial", "devreg": "~/x/devreg.py",
     "devlock_dir": "/nowhere", "settle": 30.0, "action": "read"}


@pytest.fixture
def node(monkeypatch):
    """A stock node on a port the registry knows by the name stock1."""
    n = _Node(security())
    _Lock.held, _Lock.seen = True, []

    def session(p):
        return mi.stocknode.Session(
            p["transport"], p["board"], device=p.get("device"),
            guard=lambda name: ("/dev/ttyACM7", None) if name == "stock1" else (None, f"devreg cannot resolve {name}"),
            lock=_Lock, opener=lambda s: _Iface(n))
    monkeypatch.setattr(mi, "stock_session", session)
    return n


def run(params, check_mode=False):
    with pytest.raises(_Exit) as e:
        mi.stock(_Module({**P, **params}, check_mode), {**P, **params},
                 sleep=lambda _: None, clock=iter(range(0, 1000, 5)).__next__)
    return e.value.args[0]


def test_a_stock_nodes_key_is_read_from_its_guarded_locked_port(node):
    r = run({})
    assert not r["failed"] and r["node_id"] == "0x0a0b0c0d"
    assert base64.b64decode(r["private_key"]) == OLD and r["public_key_fp"] == mi.fp(b"\x09" * 32)
    assert _Lock.seen == ["/dev/ttyACM7"] and node.writes == 0


def test_a_port_that_is_not_the_board_or_is_busy_is_never_opened(node):
    with pytest.raises(mi.stocknode.NodeError, match="cannot resolve"):
        run({"device": "other"})
    _Lock.held = False
    with pytest.raises(mi.stocknode.NodeError, match="port busy"):
        run({})
    assert node.opened == 0


ESCROW = {"action": "ensure", "private_key": base64.b64encode(NEW).decode(), "node_id": "0x0A0B0C0D"}


def test_the_escrowed_key_is_given_back_and_the_node_must_report_it(node):
    assert run(ESCROW, check_mode=True)["changed"] and node.writes == 0, "a plan writes nothing"
    r = run(ESCROW)
    assert not r["failed"], r
    assert r["changed"] and node.writes == 1
    assert bytes(node.sec.private_key) == NEW and list(node.sec.admin_key) == ADMINS
    again = run(ESCROW)
    assert not again["failed"] and not again["changed"] and node.writes == 1


def test_another_devices_escrow_is_refused_before_anything_is_sent(node):
    r = run({**ESCROW, "node_id": "0x0a0b0c0e"})
    assert r["failed"] and "another device" in r["msg"] and node.writes == 0


def test_from_2_8_a_stock_nodes_id_is_its_keys_so_the_escrowed_key_is_written(node):
    # Meshtastic 2.8 derives the node number from the public key (crc32), as zephyrtastic does:
    # a node with a fresh key has a fresh id, and only the escrowed key brings the old one back.
    node.num = int(mi.fp(bytes(node.sec.public_key)), 16)
    r = run({**ESCROW, "node_id": "0x6982c090"})
    assert not r["failed"], r
    assert r["changed"] and node.writes == 1 and bytes(node.sec.private_key) == NEW


def test_a_node_that_does_not_take_the_key_is_said(node):
    node.takes = False
    r = run(ESCROW)
    assert r["failed"] and "did not take the escrowed key" in r["msg"]


def test_a_managed_node_is_refused(node):
    node.sec.is_managed = True
    r = run(ESCROW)
    assert r["failed"] and "managed" in r["msg"] and node.writes == 0


def test_a_factory_node_has_no_key_to_read_but_takes_the_escrowed_one(node):
    """Stock firmware makes its key once a region is set; a restore often meets a node with none."""
    node.sec.private_key = b""
    r = run({})
    assert r["failed"] and "no private key" in r["msg"]
    r = run(ESCROW)
    assert not r["failed"] and r["changed"] and bytes(node.sec.private_key) == NEW
