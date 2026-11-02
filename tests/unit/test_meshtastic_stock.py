"""Stock Meshtastic over its own phone API: the radio's plan and its one-message write, the
module's phases and read-back, and the session's guard, lock and retries, on fake interfaces
built from the real protobufs (no node, no port, no network)."""
import os
import pytest

pytest.importorskip("meshtastic")

from meshtastic.protobuf import channel_pb2, config_pb2, localonly_pb2  # noqa: E402

from ansible_collections.rxrbench.mesh.plugins.module_utils import (  # noqa: E402
    enums, meshrf, stocknode, stockradio)
from ansible_collections.rxrbench.mesh.plugins.modules import meshtastic_stock_config as mod  # noqa: E402
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common  # noqa: E402


class Node:
    """A stock node's config, which a write changes the way the firmware does."""

    def __init__(self, region="US", preset="LONG_FAST", power=30, tx=True):
        self.localConfig = localonly_pb2.LocalConfig()
        lora = self.localConfig.lora
        lora.region, lora.modem_preset = stockradio.REGIONS[region], stockradio.PRESETS[preset]
        lora.use_preset, lora.tx_power, lora.tx_enabled, lora.hop_limit = True, power, tx, 3
        self.localConfig.device.role = config_pb2.Config.DeviceConfig.Role.Value("CLIENT")
        ch = channel_pb2.Channel(index=0, role=1)
        ch.settings.psk = b"\x01"
        self.channels = [ch]
        self.calls, self.stored = [], localonly_pb2.LocalConfig()
        self.stored.CopyFrom(self.localConfig)

    def writeConfig(self, name):
        self.calls.append(f"config:{name}")
        if name == "lora":
            new = self.localConfig.lora
            # the firmware: a region given to an UNSET node switches its transmitter on
            if self.stored.lora.region == 0 and new.region != 0:
                new.tx_enabled = True
        self.stored.CopyFrom(self.localConfig)

    def beginSettingsTransaction(self):
        self.calls.append("begin")

    def commitSettingsTransaction(self):
        self.calls.append("commit")

    def setOwner(self, long_name, short_name):
        self.calls.append(f"owner:{long_name}/{short_name}")

    def writeChannel(self, i):
        self.calls.append(f"channel:{i}")


class Iface:
    """What the stock library's interface gives: a fresh copy of what the node stored."""

    def __init__(self, node):
        self.node = node
        self.localNode = type("L", (), {})()
        self.localNode.localConfig = localonly_pb2.LocalConfig()
        self.localNode.localConfig.CopyFrom(node.stored)
        self.localNode.channels = node.channels
        for m in ("beginSettingsTransaction", "commitSettingsTransaction", "setOwner", "writeChannel"):
            setattr(self.localNode, m, getattr(node, m))
        self.localNode.writeConfig = self._write
        self.myInfo = object()
        self.metadata = type("M", (), {"firmware_version": "2.7.15.abc"})()
        self.closed = False

    def _write(self, name):
        self.node.localConfig.CopyFrom(self.localNode.localConfig)
        self.node.writeConfig(name)

    def getMyNodeInfo(self):
        return {"user": {"id": "!0a0b0c0d", "longName": "stock", "shortName": "st"}}

    def close(self):
        self.closed = True


class Lock:
    def __init__(self, path, free=True):
        self.path, self.free, self.held = path, free, False

    def acquire(self, wait_s=0.0):
        self.held = self.free
        return self.free

    def release(self):
        self.held = False

    def holder_summary(self):
        return "pid 1 (another tool)"


def session(node, path="/dev/serial/by-id/usb-stock", guard=None, opener=None, lock=Lock):
    return stocknode.Session("serial", "st1", guard=guard or (lambda name: (path, None)),
                             opener=opener or (lambda s: Iface(node)), lock=lock)


# -- the radio, pure -----------------------------------------------------------------------

def test_radio_plan_takes_only_what_differs():
    actual = stockradio.facts_of(Node().stored.lora)
    assert actual == {"region": "US", "modem_preset": "LONG_FAST", "use_preset": True,
                      "tx_power": 30, "tx_enabled": True}
    change, diff = stockradio.plan({"region": "US", "modem_preset": "SHORT_TURBO", "tx_power": 2},
                                   actual)
    assert change == {"modem_preset": "SHORT_TURBO", "tx_power": 2}
    assert diff["before"] == {"modem_preset": "LONG_FAST", "tx_power": 30}


@pytest.mark.parametrize("bad, why", [
    ({"hop_limit": 3}, "lora_api"),
    ({"use_preset": False}, "custom modem"),
    ({"region": "MARS"}, "not a Meshtastic region"),
    ({"region": "UNSET"}, "tx_enabled: false"),
    ({"modem_preset": "WARP"}, "not a Meshtastic preset"),
    ({"tx_power": 31}, "0 to 30"),
    ({"tx_power": True}, "0 to 30"),
    ({"tx_enabled": "yes"}, "true or false"),
])
def test_radio_refuses_what_it_does_not_manage(bad, why):
    with pytest.raises(stockradio.RadioError, match=why):
        stockradio.check(bad)


def test_a_preset_does_not_replace_a_custom_modem():
    actual = {**stockradio.facts_of(Node().stored.lora), "use_preset": False}
    with pytest.raises(stockradio.RadioError, match="custom modem"):
        stockradio.plan({"modem_preset": "SHORT_FAST"}, actual)


def test_the_radio_is_one_message_with_numbers_the_library_may_not_know():
    node = Node()
    newest = max(enums.MODEM_PRESET)            # a preset newer than some library releases
    stockradio.write(Iface(node), {"modem_preset": enums.MODEM_PRESET[newest], "tx_power": 2,
                                   "tx_enabled": False})
    assert node.calls == ["config:lora"], "region, preset, power and TX in one write"
    lora = node.stored.lora
    assert (lora.modem_preset, lora.tx_power, lora.tx_enabled, lora.use_preset) == (newest, 2, False, True)
    assert lora.hop_limit == 3, "a field not declared stays as the node has it"


def test_an_unset_region_switches_tx_on_and_is_written_off_again():
    before = {"region": "UNSET", "tx_enabled": False}
    assert stockradio.followup({"region": "US", "tx_enabled": False}, before,
                               {"region": "US", "tx_enabled": True}) == {"tx_enabled": False}
    assert stockradio.followup({"region": "US", "tx_enabled": True}, before,
                               {"region": "US", "tx_enabled": True}) == {}
    assert stockradio.followup({"region": "US", "tx_enabled": False},
                               {"region": "EU_868", "tx_enabled": False},
                               {"region": "US", "tx_enabled": False}) == {}


# -- the module's plan, phases and read-back -------------------------------------------------

def facts(node):
    return stocknode.facts_of(Iface(node))


def desired(**kw):
    d = {"owner": {}, "device": {}, "lora": {}, "display": {}, "security": {}, "channels": [],
         "radio": {}}
    d.update(kw)
    return d


def test_the_radio_goes_first_and_security_last():
    node = Node()
    want = desired(radio={"modem_preset": "SHORT_TURBO"}, owner={"long_name": "bench"},
                   security={"is_managed": False, "admin_key": []})
    changes, diff = mod.plan(want, facts(node))
    assert set(changes) >= {"radio", "owner"}
    steps = mod.phases(changes)
    assert list(steps[0]) == ["radio"], "the radio by itself, first"
    assert "owner" in steps[1]
    assert diff["after"]["radio"] == {"modem_preset": "SHORT_TURBO"}


def test_nothing_declared_plans_nothing():
    changes, _ = mod.plan(desired(), facts(Node()))
    assert changes == {}


def test_left_of_waits_only_for_the_radio_fields_a_step_wrote():
    node = Node(tx=True)
    want = desired(radio={"modem_preset": "SHORT_TURBO", "tx_enabled": False})
    phase = {"radio": {"modem_preset": "SHORT_TURBO"}}
    left = mod.left_of(want, facts(node), phase)
    assert left == {"radio": {"modem_preset": "SHORT_TURBO"}}, "tx_enabled was not in this step"


def test_first_wait_leaves_out_the_transmitter_the_firmware_will_switch_on():
    want = desired(radio={"region": "US", "tx_enabled": False})
    before = {"radio": {"region": "UNSET", "tx_enabled": False}}
    phase = {"radio": {"region": "US", "tx_enabled": False}}
    assert mod.first_wait(want, before, phase) == {"radio": {"region": "US"}}
    assert mod.first_wait(want, {"radio": {"region": "EU_868"}}, phase) == phase


def test_settle_reads_until_the_node_shows_the_step():
    node = Node()
    want = desired(radio={"modem_preset": "SHORT_TURBO", "tx_power": 2})
    s = session(node)
    p = {"settle": 30.0, "open_deadline": 5.0, "connect_timeout": 5, "flush": 0}
    with s:
        mod.write(s, {"radio": {"modem_preset": "SHORT_TURBO", "tx_power": 2}}, 0, sleep=lambda _: None)
        got, left, why = mod.settle(s, want, {"radio": {"modem_preset": "SHORT_TURBO", "tx_power": 2}},
                                    p, sleep=lambda _: None)
    assert left == {} and why is None
    assert got["radio"]["modem_preset"] == "SHORT_TURBO" and got["radio"]["tx_power"] == 2


def test_the_rf_rules_judge_the_declared_radio():
    rules = {"region": "US", "max_tx_power": 10}
    refused = meshrf.check_write("meshtastic", "st1", {"region": "US", "tx_power": 20,
                                                       "tx_enabled": True}, rules)
    assert refused, "20 dBm over a 10 dBm ceiling is refused before anything is written"
    assert meshrf.check_write("meshtastic", "st1", {"region": "US", "tx_power": 20,
                                                    "tx_enabled": False}, rules) == []


# -- the session: guard, lock, retries -------------------------------------------------------

def test_the_port_is_guarded_and_locked_for_the_whole_session():
    node, locks = Node(), []

    def lock(path):
        locks.append(Lock(path))
        return locks[-1]

    s = session(node, lock=lock)
    with s:
        assert locks[0].held and locks[0].path == "/dev/serial/by-id/usb-stock"
        s.read(1.0)
        s.read(1.0)
        assert len(locks) == 1, "one lock for the run, not one per connection"
    assert not locks[0].held


def test_a_busy_port_is_refused_naming_its_holder():
    with pytest.raises(stocknode.NodeError, match="another tool"):
        with session(Node(), lock=lambda path: Lock(path, free=False)):
            pass


def test_a_port_that_fails_the_guard_is_never_opened():
    opened = []
    s = stocknode.Session("serial", "st1", guard=lambda name: (None, "devreg guard refuses"),
                          opener=lambda s: opened.append(1), lock=Lock)
    with pytest.raises(stocknode.NodeError, match="refuses"):
        with s:
            pass
    assert opened == []


def test_a_node_that_comes_back_on_another_port_is_followed_once_devreg_guards_it():
    """A restart can renumber the port (ttyACM1 -> ttyACM0, seen on a XIAO after a region
    write). devreg guards the new path as this board, so it is opened, under its own lock."""
    paths = iter(["/dev/ttyACM1", "/dev/ttyACM0", "/dev/ttyACM0"])
    s = session(Node(), guard=lambda name: (next(paths), None))
    with s:
        s.open(1.0).close()
        assert s.path == "/dev/ttyACM0" and s.lock.path == "/dev/ttyACM0"


def test_open_retries_a_node_that_is_restarting():
    node, tries = Node(), []

    def opener(s):
        tries.append(1)
        if len(tries) < 3:
            raise ConnectionResetError("restarting")
        return Iface(node)

    clock = iter(range(100))
    s = session(node, opener=opener)
    with s:
        iface = s.open(60, sleep=lambda _: None, clock=lambda: next(clock))
    assert len(tries) == 3 and iface.myInfo is not None


def test_open_gives_up_at_its_deadline_with_the_last_error():
    clock = iter(range(0, 1000, 10))

    def opener(s):
        raise ConnectionRefusedError("no API")

    s = session(Node(), opener=opener)
    with s:
        with pytest.raises(stocknode.NodeError, match="no API"):
            s.open(25, sleep=lambda _: None, clock=lambda: next(clock))


def test_tcp_checks_the_route_and_takes_no_lock():
    s = stocknode.Session("tcp", "st1", host="192.0.2.40", guard=lambda name: ("x", None),
                          opener=lambda s: Iface(Node()), lock=lambda p: pytest.fail("no lock for tcp"))
    with s:
        assert s.read(1.0)["radio"]["region"] == "US"


def test_a_usb_node_devreg_does_not_find_is_unanswered_not_failed():
    s = stocknode.Session("serial", "st1", lock=Lock, opener=lambda s: pytest.fail("not opened"),
                          guard=lambda name: (None, common.Absent("registered but NOT PRESENT")))
    with pytest.raises(stocknode.Unanswered, match="NOT PRESENT"):
        with s:
            pass


def test_a_usb_port_that_is_there_but_whose_api_never_answers_is_a_failure():
    clock = iter(range(0, 1000, 10))

    def opener(s):
        raise ConnectionRefusedError("no API")

    with session(Node(), opener=opener) as s:
        with pytest.raises(stocknode.NodeError) as e:
            s.open(25, sleep=lambda _: None, clock=lambda: next(clock))
    assert not isinstance(e.value, stocknode.Unanswered)


def tcp_session(opener):
    return stocknode.Session("tcp", "st1", host="192.0.2.40", guard=lambda name: ("x", None),
                             opener=opener, lock=lambda p: pytest.fail("no lock for tcp"))


def test_a_network_node_that_never_answers_is_unanswered():
    clock = iter(range(0, 1000, 10))

    def opener(s):
        raise TimeoutError("timed out")

    with tcp_session(opener) as s:
        with pytest.raises(stocknode.Unanswered, match="timed out"):
            s.open(25, sleep=lambda _: None, clock=lambda: next(clock))


def test_a_network_node_that_answered_once_and_is_gone_is_a_failure():
    clock = iter(range(0, 1000, 10))
    answers = iter([True])

    def opener(s):
        if next(answers, False):
            return Iface(Node())
        raise TimeoutError("timed out")

    with tcp_session(opener) as s:
        s.open(25, sleep=lambda _: None, clock=lambda: next(clock)).close()
        with pytest.raises(stocknode.NodeError) as e:
            s.open(25, sleep=lambda _: None, clock=lambda: next(clock))
    assert not isinstance(e.value, stocknode.Unanswered)


class KeyedLock:
    """devlock's semantics: a lock file per key (the board's by-id name, else the tty's name),
    and an flock that a second open of the same file does not get, in the same process too."""
    held = set()

    def __init__(self, path, key=None):
        self.path, self.lockfile, self.mine = path, key or path, False

    def acquire(self, wait_s=0.0):
        if self.lockfile in KeyedLock.held:
            return False
        KeyedLock.held.add(self.lockfile)
        self.mine = True
        return True

    def release(self):
        if self.mine:
            KeyedLock.held.discard(self.lockfile)
            self.mine = False

    def holder_summary(self):
        return "pid 1 (rxrbench-stock-meshtastic)"


def test_a_renumbered_port_with_the_same_lock_key_keeps_the_lock_it_holds():
    """ttyACM1 and ttyACM0 of one board are one by-id key: the session must not take its own
    lock again (it would find it held and wait out its deadline), and must still hold it."""
    KeyedLock.held = set()
    paths = iter(["/dev/ttyACM1", "/dev/ttyACM0", "/dev/ttyACM0"])
    s = session(Node(), guard=lambda name: (next(paths), None),
                lock=lambda path: KeyedLock(path, key="usb-XIAO_747D-if00"))
    with s:
        s.open(1.0).close()
        assert s.path == "/dev/ttyACM0" and KeyedLock.held == {"usb-XIAO_747D-if00"}
    assert KeyedLock.held == set(), "released when the session ends"


def test_a_renumbered_port_another_holds_is_waited_for_not_shared():
    KeyedLock.held = {"/dev/ttyACM0"}          # another tool has the new path's lock
    paths = iter(["/dev/ttyACM1"] + ["/dev/ttyACM0"] * 50)
    s = session(Node(), guard=lambda name: (next(paths), None), lock=KeyedLock)
    clock = iter(range(100))
    with s:
        with pytest.raises(stocknode.NodeError, match="busy"):
            s.open(5, sleep=lambda _: None, clock=lambda: next(clock))
        assert s.path == "/dev/ttyACM1", "never moved onto a port it could not lock"


# -- the lock across a renumbered port, with the real devlock (flock on real files) --------

PROBE = """
import fcntl, os, sys
fd = os.open(os.path.join(os.environ["DEVLOCK_DIR"], sys.argv[1] + ".lock"), os.O_RDWR | os.O_CREAT)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("free")
except OSError:
    print("held")
"""

HOLD = """
import fcntl, os, sys
fd = os.open(os.path.join(os.environ["DEVLOCK_DIR"], sys.argv[1] + ".lock"), os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX)
print("holding", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def real_devlock(tmp_path, monkeypatch):
    """devreg's devlock, as a bench host has it, with its lock files in a directory of this
    test's own. Which key a path locks under is the one thing set here: on a host it is the
    board's /dev/serial/by-id name (its USB serial), the same for every tty it comes up as."""
    import importlib.util
    import subprocess
    import sys
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    monkeypatch.setenv("DEVLOCK_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("devlock", os.path.join(repo, "devreg", "devlock.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setitem(sys.modules, "devlock", mod)
    keys = {}
    monkeypatch.setattr(mod, "lock_key", lambda path: keys.get(path, os.path.basename(path)))

    def other_process(key):
        """What another tool on the host finds when it tries the lock now."""
        return subprocess.run([sys.executable, "-c", PROBE, key], capture_output=True, text=True,
                              env={**os.environ, "DEVLOCK_DIR": str(tmp_path)}, check=True).stdout.strip()

    def held_elsewhere(key):
        p = subprocess.Popen([sys.executable, "-c", HOLD, key], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "DEVLOCK_DIR": str(tmp_path)})
        assert p.stdout.readline().strip() == "holding"
        return p
    return keys, other_process, held_elsewhere


def real_session(guard):
    return stocknode.Session("serial", "xiao1", guard=guard, opener=lambda s: Iface(Node()),
                             devlock_dir=os.path.join(os.path.dirname(os.path.dirname(
                                 os.path.dirname(os.path.abspath(__file__)))), "devreg"))


def test_a_renumbered_board_stays_locked_against_other_tools_throughout(real_devlock):
    """The case seen on the Pi: another board was unplugged, the XIAO restarted after a region
    write and came back as ttyACM0, not ttyACM1. Both are one board, one lock key: the session
    keeps its lock (no wait on itself), and no other process gets the board at any point."""
    keys, other_process, _ = real_devlock
    key = "usb-Board_0A0B0C0D0E0F1011-if00"
    keys.update({"/dev/ttyACM1": key, "/dev/ttyACM0": key})
    paths = iter(["/dev/ttyACM1", "/dev/ttyACM1", "/dev/ttyACM0", "/dev/ttyACM0"])
    s = real_session(lambda name: (next(paths), None))

    def no_waiting(_):
        raise AssertionError("waited: the session found its own lock held")
    with s:
        assert other_process(key) == "held"
        s.open(5, sleep=no_waiting).close()          # before the restart
        s.open(5, sleep=no_waiting).close()          # after it: renumbered
        assert s.path == "/dev/ttyACM0"
        assert other_process(key) == "held", "still this session's, under the board's key"
    assert other_process(key) == "free", "released when the session ends"


def test_a_new_port_another_tool_holds_is_waited_for_and_never_shared(real_devlock):
    """Where the new tty locks under another key (no by-id link yet), and another process holds
    it: the session does not open it, keeps the board's old lock, and moves once it is free."""
    keys, other_process, held_elsewhere = real_devlock
    keys.update({"/dev/ttyACM1": "ttyACM1", "/dev/ttyACM0": "ttyACM0"})
    paths = iter(["/dev/ttyACM1"] + ["/dev/ttyACM0"] * 20)
    s = real_session(lambda name: (next(paths), None))
    holder = held_elsewhere("ttyACM0")
    clock = iter(range(1000))
    try:
        with s:
            with pytest.raises(stocknode.NodeError, match="busy"):
                s.open(4, sleep=lambda _: None, clock=lambda: next(clock))
            assert s.path == "/dev/ttyACM1" and other_process("ttyACM1") == "held"
            holder.stdin.close()
            holder.wait(10)
            s.open(4, sleep=lambda _: None, clock=lambda: next(clock)).close()
            assert s.path == "/dev/ttyACM0"
            assert other_process("ttyACM0") == "held" and other_process("ttyACM1") == "free"
        assert other_process("ttyACM0") == "free"
    finally:
        holder.kill()


def test_a_library_that_hangs_on_open_stops_the_run_within_its_deadline(monkeypatch):
    """Seen on a XIAO right after a factory reset: the stock library sat 7 min in the open, past
    the module's deadline. The open has its own deadline, and a hang is not retried (the stuck
    library may still hold the port)."""
    import threading
    import time
    forever, tries = threading.Event(), []
    monkeypatch.setattr(stocknode, "HANG_MARGIN", 0.3)

    def opener(s):
        tries.append(1)
        forever.wait()
    s = stocknode.Session("serial", "st1", guard=lambda name: ("/dev/ttyACM0", None),
                          opener=opener, lock=Lock, timeout=0.2)
    t0 = time.monotonic()
    with s:
        with pytest.raises(stocknode.Hung, match="did not return within"):
            s.open(60)
    assert time.monotonic() - t0 < 5 and tries == [1]
    forever.set()


def test_a_port_held_by_a_broker_says_to_run_hosts_setup():
    """A board switched to stock by apply before hosts-setup ran: the old family's broker holds the
    port, and the config step says what to run."""
    class BrokerLock(Lock):
        def holder_summary(self):
            return "rxrbench-broker (pid 9, pi@bench) since 2026-11-03T21:15:00Z (2s) [long hold declared]"
    with pytest.raises(stocknode.NodeError, match=r"port busy: rxrbench-broker .*just hosts-setup"):
        with session(Node(), lock=lambda path: BrokerLock(path, free=False)):
            pass
