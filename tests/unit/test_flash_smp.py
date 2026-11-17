"""The MCUmgr flasher's decisions, with the MCUmgr sessions themselves replaced: what it
refuses, and what it makes of a device that swapped back or could not be confirmed; and the
two things only the BLE transport needs, the adapter lock and the stale-bond repair."""
import os
import sys
import types

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, delivery, smp

DEVLOCK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "devreg")

NEW, OLD = "aa" * 32, "bb" * 32
# The flasher's own behaviour, on the fixture plugin's widget family (a Zephyr shell).
pytestmark = pytest.mark.usefixtures("widgets")

MANIFEST = {"family": "widget", "format": "uf2", "file": "app.uf2", "board_tag": "xiao",
            "class": 1, "version": "0.4.40", "hash": NEW, "build": "new11111"}


def image(i, active, confirmed=True):
    return {"slot": 0 if active else 1, "version": "0.4.40" if i == NEW else "0.4.39",
            "hash": i, "active": active,
            "confirmed": confirmed, "pending": False}


class Scripted(smp.SmpFlasher):
    """Each MCUmgr session returns the next scripted image-state list."""

    def __init__(self, tmp_path, answers, **kw):
        (tmp_path / "app.signed.bin").write_bytes(b"\0" * 64)
        super().__init__("board7", MANIFEST, str(tmp_path), board_tag="xiao", declared={"family": "widget", "class": 1},
                         settle=30, **kw)
        self.answers = list(answers)
        self.sessions = 0

    def _session(self, work, timeout=60.0):  # pylint: disable=unused-argument
        self.sessions += 1
        return self.answers.pop(0)

    def write(self):
        self.sessions += 1


@pytest.fixture(autouse=True)
def no_sleep_and_a_stand_in_library(monkeypatch):
    """smpclient lives on the bench hosts, not in this repo's venv: a stand-in is enough
    for the decisions, which never reach it here."""
    import sys
    import types
    monkeypatch.setattr(smp.time, "sleep", lambda s: None)
    for name in ("smpclient", "smpclient.requests", "smpclient.requests.image_management",
                 "smpclient.requests.os_management"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    im = sys.modules["smpclient.requests.image_management"]
    monkeypatch.setattr(im, "ImageStatesWrite", object, raising=False)
    monkeypatch.setattr(im, "ImageStatesRead", object, raising=False)


def test_a_device_running_the_image_is_not_touched(tmp_path):
    f = Scripted(tmp_path, [[image(NEW, True)]])
    r = common.drive(f, MANIFEST)
    assert not r["changed"] and f.sessions == 1


def test_an_update_ends_with_the_new_image_running_and_confirmed(tmp_path):
    f = Scripted(tmp_path, [[image(OLD, True)], [image(NEW, True), image(OLD, False)]])
    r = common.drive(f, MANIFEST)
    assert r["changed"] and r["after"]["hash"] == NEW and f.flags["confirmed"]


def test_a_swap_back_is_reported_as_one(tmp_path):
    f = Scripted(tmp_path, [[image(OLD, True)], [image(OLD, True), image(NEW, False)]])
    with pytest.raises(common.FlashError, match="runs its previous image"):
        common.drive(f, MANIFEST)


def test_an_image_that_runs_but_is_not_confirmed_is_an_error(tmp_path):
    f = Scripted(tmp_path, [[image(OLD, True)], [image(NEW, True, confirmed=False)]])
    with pytest.raises(common.FlashError, match="could not be confirmed"):
        common.drive(f, MANIFEST)


def test_a_device_still_on_trial_is_not_given_another_update(tmp_path):
    f = Scripted(tmp_path, [[image(OLD, True, confirmed=False)]])
    with pytest.raises(common.FlashError, match="still on trial"):
        common.drive(f, MANIFEST)
    assert f.sessions == 1


def test_an_image_for_another_board_or_without_its_signed_file_is_refused(tmp_path):
    f = Scripted(tmp_path, [[image(OLD, True)]])
    f.board_tag = "heltec-v4"
    (tmp_path / "app.signed.bin").unlink()
    with pytest.raises(common.FlashError) as e:
        common.drive(f, MANIFEST)
    assert "this board is a 'heltec-v4'" in str(e.value) and "no app.signed.bin" in str(e.value)
    assert f.sessions == 1


def test_a_route_the_image_has_no_mcumgr_on_is_refused_before_any_session(tmp_path):
    """A serial plan for a class 4 build (MCUmgr over BLE only) is refused, not left waiting for an
    answer that cannot come."""
    f = Scripted(tmp_path, [[image(OLD, True)]])
    f.manifest = {**MANIFEST, "mcumgr": ["ble"]}
    with pytest.raises(common.FlashError, match="no MCUmgr over serial .it has ble."):
        common.drive(f, f.manifest)
    assert f.sessions == 0, "refused before a byte is sent"
    f = Scripted(tmp_path, [[image(NEW, True)]])
    f.manifest = {**MANIFEST, "mcumgr": ["ble", "serial"]}
    assert common.drive(f, f.manifest)["changed"] is False


# ---- BLE: the host must not hold the node's last connection slot ---------------------

def ble_flasher(tmp_path, **kw):
    (tmp_path / "app.signed.bin").write_bytes(b"\0" * 64)
    os.environ["XDG_RUNTIME_DIR"] = str(tmp_path)     # the lock directory, for this test
    return smp.SmpFlasher("board2", MANIFEST, str(tmp_path), transport="ble",
                          address="02:00:00:00:00:12", devlock_dir=DEVLOCK_DIR, **kw)


def test_a_link_the_host_already_holds_is_dropped_before_a_ble_session(tmp_path, monkeypatch):
    calls = []

    def tool(argv, timeout, **kw):  # pylint: disable=unused-argument
        calls.append(argv[3])
        return 0, "Device 02:00:00:00:00:12\n\tConnected: yes\n" if argv[3] == "info" else ""
    monkeypatch.setattr(smp.common, "tool", tool)
    ble_flasher(tmp_path)._free_ble_slot()
    assert calls == ["info", "disconnect"]


def test_nothing_is_dropped_and_nothing_is_pre_connected_when_the_host_holds_no_link(
        tmp_path, monkeypatch):
    calls = []

    def tool(argv, timeout, **kw):  # pylint: disable=unused-argument
        calls.append(argv[3])
        return 0, "\tConnected: no\n"
    monkeypatch.setattr(smp.common, "tool", tool)
    ble_flasher(tmp_path)._free_ble_slot()
    assert calls == ["info"]


class FlakyClient:
    """Fails its first `fails` connects, as bleak does from cold. Its connect takes what
    smpclient's does (`connect_timeout_s`, defaulting to the request timeout), and records it."""
    made = 0
    caps = []

    def __init__(self, fails):
        self.fails = fails

    async def connect(self, connect_timeout_s=None):
        FlakyClient.made += 1
        FlakyClient.caps.append(connect_timeout_s)
        if FlakyClient.made <= self.fails:
            raise RuntimeError("failed to discover services, device disconnected")

    async def disconnect(self):
        pass


@pytest.mark.parametrize("fails, ok", [(0, True), (1, True), (2, False)])
def test_a_ble_connect_is_tried_twice_and_then_reported(tmp_path, monkeypatch, fails, ok):
    FlakyClient.made = 0
    f = ble_flasher(tmp_path)
    monkeypatch.setattr(f, "_free_ble_slot", lambda: None)
    monkeypatch.setattr(f, "_client", lambda: FlakyClient(fails))
    monkeypatch.setattr(smp.asyncio, "sleep", lambda s: FlakyClient(0).disconnect())

    async def work(client):  # pylint: disable=unused-argument
        return "done"
    if ok:
        assert f._session(work) == "done"
    else:
        with pytest.raises(smp.ConnectError, match="cannot connect: RuntimeError failed to discover"):
            f._session(work)
    assert FlakyClient.made == min(fails + 1, smp.BLE_CONNECT_TRIES)


def test_a_link_dropped_mid_session_is_a_flash_error_the_verify_loop_can_retry(tmp_path, monkeypatch):
    """smpclient reports a link lost during a request with its own Exception subclass; it must
    reach the module as a FlashError, not a crash, even after a confirm write went through."""
    class Dropped(Exception):
        pass
    tr = types.ModuleType("smpclient.transport")
    tr.SMPTransportDisconnected = Dropped
    monkeypatch.setitem(sys.modules, "smpclient.transport", tr)
    f = ble_flasher(tmp_path)
    monkeypatch.setattr(f, "_free_ble_slot", lambda: None)
    monkeypatch.setattr(f, "_client", lambda: FlakyClient(0))

    async def work(client):  # pylint: disable=unused-argument
        raise Dropped("SMPBLETransport disconnected from AA:BB")
    with pytest.raises(common.FlashError, match="MCUmgr over ble: Dropped SMPBLETransport disconnected"):
        f._session(work)
    assert _devlock().probe("ble:hci0") is None


def _devlock():
    if DEVLOCK_DIR not in sys.path:
        sys.path.insert(0, DEVLOCK_DIR)
    import devlock  # pylint: disable=import-error,import-outside-toplevel
    return devlock


def test_the_adapter_is_held_for_a_ble_session_and_free_after(tmp_path, monkeypatch):
    f = ble_flasher(tmp_path)
    devlock = _devlock()
    monkeypatch.setattr(f, "_free_ble_slot", lambda: None)
    monkeypatch.setattr(f, "_client", lambda: FlakyClient(0))
    seen = {}

    async def work(client):  # pylint: disable=unused-argument
        seen["holder"] = devlock.probe("ble:hci0")
        return "done"
    assert f._session(work) == "done"
    assert seen["holder"]["tool"] == "rxrbench-smp_flash" and seen["holder"]["device"] == "board2"
    assert devlock.probe("ble:hci0") is None


def test_a_busy_adapter_is_refused_naming_its_holder_and_no_session_is_opened(tmp_path, monkeypatch):
    f = ble_flasher(tmp_path, ble_lock_wait=0.3)
    devlock = _devlock()
    opened = []
    monkeypatch.setattr(f, "_free_ble_slot", lambda: opened.append("freed"))
    monkeypatch.setattr(f, "_client", lambda: opened.append("client"))
    with devlock.DevLock("ble:hci0", tool="other-tool"):
        with pytest.raises(common.FlashError, match="adapter hci0 is busy: other-tool"):
            f._session(lambda c: None)
    assert opened == []
    assert devlock.probe("ble:hci0") is None


def test_a_ble_address_that_is_not_the_registrys_is_refused_before_any_session(tmp_path, monkeypatch):
    """The address comes from the inventory; the registry on the host has the last word."""
    devreg = os.path.join(DEVLOCK_DIR, "devreg.py")
    reg = tmp_path / "registry.yaml"
    reg.write_text('version: 1\nhosts: {}\ndevices:\n  board2:\n    kind: serial\n'
                   '    match: {usb_serial: "00"}\n'
                   '    reach: {ble: {addr: "02:00:00:00:00:12"}}\n'
                   '  board3:\n    kind: serial\n    match: {usb_serial: "01"}\n')
    monkeypatch.setenv("DEVREG_REGISTRY", str(reg))
    opened = []
    monkeypatch.setitem(sys.modules, "smpclient", types.SimpleNamespace(
        SMPClient=lambda *a, **kw: opened.append(a)))
    ble = types.ModuleType("smpclient.transport.ble")
    ble.SMPBLETransport = object
    monkeypatch.setitem(sys.modules, "smpclient.transport", types.ModuleType("smpclient.transport"))
    monkeypatch.setitem(sys.modules, "smpclient.transport.ble", ble)

    def flasher(name, address):
        (tmp_path / "app.signed.bin").write_bytes(b"\0" * 64)
        return smp.SmpFlasher(name, MANIFEST, str(tmp_path), transport="ble", address=address,
                              devreg=devreg, devlock_dir=DEVLOCK_DIR)

    for name, address, why in (("board2", "02:00:00:00:00:99", "not 02:00:00:00:00:99"),
                               ("board3", "02:00:00:00:00:12", "no ble route"),
                               ("board2", None, "no ble address")):
        with pytest.raises(common.FlashError, match=why):
            flasher(name, address)._client()   # pylint: disable=protected-access
    assert not opened
    f = flasher("board2", "02:00:00:00:00:12")
    f._client()   # pylint: disable=protected-access
    assert len(opened) == 1 and opened[0][1] == "02:00:00:00:00:12" and f.route_checked


HOST = "02:00:00:00:00:99"
HELD = "bt/keys/aca704e14bb60\nbt/keys/0200000000990"     # another peer's, then this host's
NOT_HELD = "bt/keys/aca704e14bb60"


class Console:
    """The board's broker: answers the bonds command as scripted, or not at all."""

    def __init__(self, bonds):
        self.bonds, self.asked = bonds, []

    def exec(self, cmd, deadline=8.0):  # pylint: disable=unused-argument
        self.asked.append(cmd)
        if self.bonds is None:
            raise smp.broker_client.BrokerError("board2: port closed")
        return self.bonds


class BondScripted(smp.SmpFlasher):
    """Sessions fail or answer as scripted; bluetoothctl's answers and the board's are
    scripted too. `bonds` is what the board's console lists; with none given the board
    has no console."""

    def __init__(self, tmp_path, sessions, paired, bonds="no console", **kw):
        (tmp_path / "app.signed.bin").write_bytes(b"\0" * 64)
        console = None if bonds == "no console" else Console(bonds)
        super().__init__("board2", MANIFEST, str(tmp_path), transport="ble",
                         address="02:00:00:00:00:12", devlock_dir=DEVLOCK_DIR,
                         console=console, **kw)
        self.answers, self.paired, self.calls = list(sessions), paired, []

    def _session(self, work, timeout=60.0):  # pylint: disable=unused-argument
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def _bluez_paired(self):
        self.calls.append("info")
        return self.paired

    def _remove_bond(self):
        self.calls.append("remove")

    def _host_address(self):
        return HOST


def test_a_bond_the_board_no_longer_holds_is_removed_once_and_the_retry_answers(tmp_path):
    f = BondScripted(tmp_path, [common.FlashError("MCUmgr over ble: TimeoutError"),
                                [image(NEW, True)]], paired=True, bonds=NOT_HELD)
    assert f.device()["hash"] == NEW
    assert f.calls == ["info", "remove"] and f.console.asked == ["settings list bt/keys"]
    assert "was stale" in f.rebonded and "TimeoutError" in f.rebonded


def test_a_bond_the_board_holds_too_is_kept_and_the_link_is_blamed(tmp_path):
    # Connected, then no answer: a stale key and a dropped link look the same from the host.
    # The board holds the bond, so it was the link. Removing the host's half here would leave
    # a board refusing a new pairing over the half it kept.
    f = BondScripted(tmp_path, [common.FlashError("MCUmgr over ble: TimeoutError")],
                     paired=True, bonds=HELD)
    with pytest.raises(common.FlashError, match="TimeoutError .*on the board too.*nothing was removed"):
        f.device()
    assert f.calls == ["info"] and f.rebonded is None


@pytest.mark.parametrize("bonds", ["no console", None])
def test_a_board_that_cannot_be_asked_keeps_its_bond(tmp_path, bonds):
    f = BondScripted(tmp_path, [common.FlashError("MCUmgr over ble: TimeoutError")],
                     paired=True, bonds=bonds)
    with pytest.raises(common.FlashError, match="could not be asked.*rebond: force"):
        f.device()
    assert f.calls == ["info"] and f.rebonded is None


def test_rebond_force_removes_the_bond_without_asking_the_board(tmp_path):
    f = BondScripted(tmp_path, [common.FlashError("MCUmgr over ble: TimeoutError"),
                                [image(NEW, True)]], paired=True, bonds=HELD, rebond="force")
    assert f.device()["hash"] == NEW
    assert f.calls == ["info", "remove"] and f.console.asked == []


def test_a_bond_only_the_board_holds_is_named_as_the_cause(tmp_path):
    f = BondScripted(tmp_path, [common.FlashError("MCUmgr over ble: BleakError")],
                     paired=False, bonds=HELD)
    with pytest.raises(common.FlashError, match="bt/keys/0200000000990.*this host no longer has"):
        f.device()
    assert f.calls == ["info"]


@pytest.mark.parametrize("bonds, rebond", [(HELD, "auto"), ("no console", "auto"), (None, "auto"),
                                           ("no console", "force"), (HELD, "force")])
def test_a_device_that_cannot_be_connected_keeps_its_bond(tmp_path, bonds, rebond):
    # A dead link is radio, not keys: the bond was good on both sides. Without the board's
    # word that it holds none, a connect failure removes nothing, whatever `rebond` says.
    f = BondScripted(tmp_path, [smp.ConnectError("MCUmgr over ble: cannot connect: TimeoutError")],
                     paired=True, bonds=bonds, rebond=rebond)
    with pytest.raises(smp.ConnectError, match="cannot connect: TimeoutError$"):
        f.device()
    assert "remove" not in f.calls and f.rebonded is None


def test_a_stale_bond_that_fails_the_connect_is_repaired_on_the_boards_word(tmp_path):
    # How a real stale bond showed on the bench: BlueZ encrypts on its key as soon as the
    # link is up, the board has no such key and drops it, service discovery fails.
    why = "MCUmgr over ble: cannot connect: BleakError failed to discover services, device disconnected"
    f = BondScripted(tmp_path, [smp.ConnectError(why), [image(NEW, True)]], paired=True, bonds=NOT_HELD)
    assert f.device()["hash"] == NEW
    assert f.calls == ["info", "remove"] and "was stale" in f.rebonded


def test_a_connect_failure_on_a_device_the_host_never_paired_asks_nothing(tmp_path):
    f = BondScripted(tmp_path, [smp.ConnectError("cannot connect: TimeoutError")], paired=False,
                     bonds=NOT_HELD)
    with pytest.raises(smp.ConnectError):
        f.device()
    assert f.calls == ["info"] and f.console.asked == []


def test_no_bond_means_no_repair_and_the_first_error_stands(tmp_path):
    f = BondScripted(tmp_path, [common.FlashError("cannot connect: out of range")], paired=False)
    with pytest.raises(common.FlashError, match="out of range$"):
        f.device()
    assert f.calls == ["info"] and f.rebonded is None


def test_rebond_never_leaves_the_bond_alone(tmp_path):
    f = BondScripted(tmp_path, [common.FlashError("TimeoutError")], paired=True, bonds=NOT_HELD,
                     rebond="never")
    with pytest.raises(common.FlashError, match="TimeoutError"):
        f.device()
    assert f.calls == [] and f.console.asked == []


def test_a_second_failure_after_the_repair_reports_both(tmp_path):
    f = BondScripted(tmp_path, [common.FlashError("first: TimeoutError"),
                                common.FlashError("second: TimeoutError")], paired=True,
                     bonds=NOT_HELD)
    with pytest.raises(common.FlashError, match="second: TimeoutError .after removing .* first"):
        f.device()
    assert f.calls == ["info", "remove"] and f.rebonded is None


def test_after_a_ble_update_the_console_is_waited_for(tmp_path, monkeypatch):
    # The device answers MCUmgr before its USB console is back with the broker: the task
    # after the flash read the board and found the port closed, on both legs of a round trip.
    waited = []
    monkeypatch.setattr(smp.common, "wait_back",
                        lambda board, family, settle, deadline=10.0: waited.append((board, family)))
    f = BondScripted(tmp_path, [[image(NEW, True)]], paired=True, bonds=HELD)
    assert f.verify()["hash"] == NEW
    assert waited == [(f.console, "widget")]

    def gone(board, family, settle, deadline=10.0):
        raise common.FlashError("after the flash, 240 s: port closed")
    monkeypatch.setattr(smp.common, "wait_back", gone)
    f = BondScripted(tmp_path, [[image(NEW, True)]], paired=True, bonds=HELD)
    with pytest.raises(common.FlashError, match="confirmed, but the board's console did not come back"):
        f.verify()


def test_the_hosts_own_address_is_read_from_bluez(tmp_path, monkeypatch):
    f = BondScripted(tmp_path, [], paired=True, bonds=HELD)
    monkeypatch.setattr(smp.common, "tool", lambda argv, timeout: (
        0, "Controller 02:00:00:00:00:99 (public)\n\tName: bench\n\tPowered: yes\n"))
    assert smp.SmpFlasher._host_address(f) == HOST
    assert smp.SmpFlasher._board_bond(f) == "bt/keys/0200000000990"
    monkeypatch.setattr(smp.common, "tool", lambda argv, timeout: (1, "No default controller available"))
    assert smp.SmpFlasher._host_address(f) is None
    f._host_address = lambda: None
    assert f._board_bond() is None and f.console.asked == ["settings list bt/keys"]


def test_the_serial_transport_never_touches_bonds(tmp_path):
    (tmp_path / "app.signed.bin").write_bytes(b"\0" * 64)
    f = Scripted(tmp_path, [])
    f._session = lambda work, timeout=60.0: (_ for _ in ()).throw(common.FlashError("gone"))
    f._bluez_paired = lambda: (_ for _ in ()).throw(AssertionError("asked BlueZ"))
    with pytest.raises(common.FlashError, match="gone"):
        f.device()


def test_a_ble_connect_is_given_longer_than_a_request(tmp_path, monkeypatch):
    """smpclient caps scan, link and discovery at the client's request timeout (10 s) unless
    connect is told otherwise; a host whose controller aborts a few LE connects before one
    holds runs out of it, so connect is given the module's own wait."""
    FlakyClient.made, FlakyClient.caps = 0, []
    f = ble_flasher(tmp_path)
    monkeypatch.setattr(f, "_free_ble_slot", lambda: None)
    monkeypatch.setattr(f, "_client", lambda: FlakyClient(0))

    async def work(client):  # pylint: disable=unused-argument
        return "done"
    assert f._session(work) == "done"
    assert FlakyClient.caps == [smp.BLE_CONNECT_S] and smp.BLE_CONNECT_S >= 30


# ---- UDP: a board on a network (reach.eth or reach.wifi) ------------------------------

class UDPAt:
    """smpclient's UDP transport stand-in: records the port its connect was given."""
    connected = []

    def __init__(self, mtu=1500):
        self.mtu = mtu

    async def connect(self, address, timeout_s, port=1337):
        UDPAt.connected.append((address, port))


def test_a_udp_address_that_is_not_the_registrys_is_refused_and_the_port_is_the_routes(
        tmp_path, monkeypatch):
    """The board's IP comes from the inventory; the registry's eth route has the last word, as
    for a BLE address. The route's port is the one connected to."""
    devreg = os.path.join(DEVLOCK_DIR, "devreg.py")
    reg = tmp_path / "registry.yaml"
    reg.write_text('version: 1\nhosts: {}\ndevices:\n  nf7:\n    kind: swd\n'
                   '    reach: {eth: {ip: "192.0.2.190", port: 1400, state: live, can: [flash]}}\n')
    monkeypatch.setenv("DEVREG_REGISTRY", str(reg))
    opened = []
    monkeypatch.setitem(sys.modules, "smpclient", types.SimpleNamespace(
        SMPClient=lambda transport, address, **kw: opened.append((transport, address)) or transport))
    udp = types.ModuleType("smpclient.transport.udp")
    udp.SMPUDPTransport = UDPAt
    monkeypatch.setitem(sys.modules, "smpclient.transport", types.ModuleType("smpclient.transport"))
    monkeypatch.setitem(sys.modules, "smpclient.transport.udp", udp)
    (tmp_path / "app.signed.bin").write_bytes(b"\0" * 64)

    def flasher(address, bearer="eth"):
        return smp.SmpFlasher("nf7", MANIFEST, str(tmp_path), transport="udp", address=address,
                              bearer=bearer, udp_port=1400, devreg=devreg, devlock_dir=DEVLOCK_DIR)

    for address, why in (("192.0.2.99", "not 192.0.2.99"), (None, "no eth address")):
        with pytest.raises(common.FlashError, match=why):
            flasher(address)._client()   # pylint: disable=protected-access
    assert not opened
    f = flasher("192.0.2.190")
    transport = f._client()   # pylint: disable=protected-access
    assert opened[0][1] == "192.0.2.190" and f.route_checked
    import asyncio
    asyncio.run(transport.connect("192.0.2.190", 5))
    assert UDPAt.connected[-1] == ("192.0.2.190", 1400), "the route's port, not the library's"


def test_udp_goes_over_a_network_route_and_needs_the_images_udp_transport(tmp_path):
    (tmp_path / "app.signed.bin").write_bytes(b"\0" * 64)
    f = smp.SmpFlasher("nf7", {**MANIFEST, "mcumgr": ["serial"]}, str(tmp_path), transport="udp",
                       address="192.0.2.190", bearer="swd", board_tag="xiao",
                       declared={"family": "widget", "class": 1})
    why = " ".join(f.preflight())
    assert "network route (eth, wifi), not swd" in why
    assert "no MCUmgr over udp (it has serial)" in why


def _frame(seq, body=b"\xa0"):
    """An SMP frame: an 8-byte header (sequence in the seventh byte) and a body."""
    return bytes([1, 0, 0, len(body), 0, 1, seq, 0]) + body


def test_udp_drops_a_reply_to_an_earlier_request_and_waits_for_its_own():
    import asyncio
    sent, replies = [], [_frame(106, b"\xa1old"), _frame(107, b"\xa1new")]

    async def send(data):
        sent.append(data)

    async def receive():
        return replies.pop(0)

    got = asyncio.run(smp._reply_to(send, receive, _frame(107)))
    assert got == _frame(107, b"\xa1new")
    assert sent == [_frame(107)] and not replies


def test_udp_takes_the_first_reply_when_it_is_its_own():
    import asyncio
    replies = [_frame(5), _frame(6)]

    async def send(_data):
        return None

    async def receive():
        return replies.pop(0)

    assert asyncio.run(smp._reply_to(send, receive, _frame(5))) == _frame(5)
    assert replies == [_frame(6)]


def test_udp_sends_the_request_again_when_its_reply_is_lost():
    import asyncio
    sent = []
    replies = asyncio.Queue()

    async def send(data):
        sent.append(data)
        if len(sent) == 2:          # the first request (or its reply) was lost
            await replies.put(_frame(9))

    async def receive():
        return await replies.get()

    got = asyncio.run(smp._reply_to(send, receive, _frame(9), resend_s=0.05))
    assert got == _frame(9) and sent == [_frame(9), _frame(9)]


def test_an_upload_start_is_never_sent_twice():
    start = bytes([2, 0, 0, 12, 0, 1, 3, 1]) + b"\xa2clen\x1a\x00\x08\x99\xa0coff\x00"
    chunk = bytes([2, 0, 0, 12, 0, 1, 4, 1]) + b"\xa2coff\x1a\x00\x00\x02\x00ddata"
    assert smp._upload_start(start)
    assert not smp._upload_start(chunk)
    assert not smp._upload_start(_frame(1))


# ---- the write handed to a route that carries the image itself ---------------------------------

class Route:
    """A delivering route (flash/delivery.py), scripted: what it says of the device each time
    it is asked, and what it was told to deliver."""
    bearer, target, credential = "courier", "dev1", None
    blockers = delivery.Delivery.blockers          # the real rule, over this route's answers

    def __init__(self, *says, refuses=()):
        self.says, self.refuses, self.told = list(says), list(refuses), []

    def refusals(self, manifest):
        assert manifest["format"] == "mcuboot-bin"      # what the flasher would have uploaded
        return list(self.refuses)

    def state(self):
        return self.says.pop(0) if len(self.says) > 1 else self.says[0]

    def deliver(self, image, manifest, name=None):
        self.told.append((os.path.basename(image), manifest["format"], name))
        return {"id": 7, "already": False}


IDLE = {"known": True, "open": False, "result": None, "detail": "nothing assigned yet"}
OPEN = {"known": True, "open": True, "result": None, "detail": "action 7 retrieved"}
DONE = {"known": True, "open": False, "result": "success", "detail": "action 7 finished"}
FAILED = {"known": True, "open": False, "result": "failure", "detail": "action 7 error: no room in the slot"}


class Delivered(Scripted):
    """The scripted flasher with its real write, and a last answer that repeats: a device is
    looked at as often as the wait asks."""
    write = smp.SmpFlasher.write

    def _session(self, work, timeout=60.0):  # pylint: disable=unused-argument
        self.sessions += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.fixture
def clock(monkeypatch):
    """Time that passes only while the flasher sleeps, so a wait ends at once."""
    now = [0.0]
    monkeypatch.setattr(smp.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(smp.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def test_a_delivered_image_is_fetched_by_the_device_and_confirmed_by_it(tmp_path, clock):
    route = Route(IDLE, OPEN, OPEN, DONE)
    f = Delivered(tmp_path, [[image(OLD, True)],                       # what it runs
                             [image(OLD, True)],                       # still fetching
                             [image(NEW, True, confirmed=False)],      # swapped in: the write is over
                             [image(NEW, True, confirmed=False)],      # on trial
                             [image(NEW, True), image(OLD, False)]],   # it confirmed itself
                  delivery=route)
    r = common.drive(f, MANIFEST)
    assert r["changed"] and r["after"]["hash"] == NEW and f.flags["confirmed"]
    assert route.told == [("app.signed.bin", "mcuboot-bin", tmp_path.name)]
    assert f.delivered == {"id": 7, "already": False}


def test_a_plan_tells_the_route_nothing(tmp_path, clock):
    route = Route(IDLE)
    f = Delivered(tmp_path, [[image(OLD, True)]], delivery=route)
    r = common.drive(f, MANIFEST, check_mode=True)
    assert r["changed"] and route.told == []


def test_a_device_that_never_confirms_a_delivered_image_is_an_error_and_is_not_confirmed_for_it(tmp_path, clock):
    route = Route(IDLE, OPEN)
    f = Delivered(tmp_path, [[image(OLD, True)], [image(NEW, True, confirmed=False)]], delivery=route)
    with pytest.raises(common.FlashError, match="the device has not confirmed it"):
        common.drive(f, MANIFEST)


def test_a_delivery_that_fails_on_the_device_ends_the_wait_with_the_routes_words(tmp_path, clock):
    route = Route(IDLE, OPEN, FAILED)
    f = Delivered(tmp_path, [[image(OLD, True)]], delivery=route)
    with pytest.raises(common.FlashError, match="failed before the device ran the image.*no room in the slot"):
        common.drive(f, MANIFEST)


def test_a_route_told_of_a_success_does_not_make_a_swapped_back_device_updated(tmp_path, clock):
    """The device took the image, it did not hold, MCUboot swapped back, and the old image's
    client told the route the update was done. The device is read, and says otherwise."""
    route = Route(IDLE, OPEN, DONE)
    f = Delivered(tmp_path, [[image(OLD, True)], [image(OLD, True), image(NEW, False)]], delivery=route)
    with pytest.raises(common.FlashError, match="runs its previous image"):
        common.drive(f, MANIFEST)


def test_a_device_that_never_asks_times_the_delivery_out(tmp_path, clock):
    route = Route(IDLE, OPEN)
    f = Delivered(tmp_path, [[image(OLD, True)]], delivery=route, delivery_timeout=60)
    with pytest.raises(common.FlashError, match="did not take the courier delivery and run the image within 60 s"):
        common.drive(f, MANIFEST)


def test_a_route_that_cannot_carry_it_or_does_not_know_the_device_refuses_before_anything(tmp_path, clock):
    for route, said in ((Route(IDLE, refuses=["its flash route courier carries raucb"]), "carries raucb"),
                        (Route({"known": False, "open": False}), "does not know the target 'dev1'"),
                        (Route(OPEN), "still has a delivery in flight")):
        f = Delivered(tmp_path, [[image(OLD, True)]], delivery=route)
        with pytest.raises(common.FlashError, match=said):
            common.drive(f, MANIFEST)
        assert route.told == []
