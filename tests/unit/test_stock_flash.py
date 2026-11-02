"""A UF2 board going to, or staying on, firmware with no state line (stock Meshtastic): what it
runs is read by its family's probe, it has no broker, and a switch from a console family is a
plain application copy."""
import json
import sys

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, familyspec, identity
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, uf2
from tests.unit.test_flash_wired import FakeBoard, rig, script  # noqa: F401  (rig is a fixture)


@pytest.fixture
def widgets():
    """The rig these tests borrow (test_flash_wired) loads the fixture plugin; these tests are
    of the mesh plugin's stock families on a bench that has only the bundled plugins."""
    yield None


class _Lock:
    held, seen = True, []

    def __init__(self, port, tool=None):
        _Lock.seen.append((port, tool))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def holder_summary(self):
        return "rxrbench-broker (pid 9)"


@pytest.fixture
def probe(monkeypatch):
    """A bench host: devreg knows board9's port, devlock is importable, the probe is faked."""
    monkeypatch.setitem(sys.modules, "devlock", type("D", (), {"DevLock": _Lock}))
    _Lock.held, _Lock.seen = True, []
    monkeypatch.setattr(common, "guarded_port",
                        lambda devreg, name: ("/dev/ttyACM3", None) if name == "board9"
                        else (None, f"devreg cannot resolve {name}"))
    ran, answer = [], [(0, '{"firmware_version": "2.7.26.54e0d8d", "hw_model": "XIAO_NRF52_KIT"}\n')]

    def run(argv, timeout):
        ran.append((argv, timeout))
        return answer[0]
    return ran, run, answer


def spec():
    return familyspec.of("meshtastic")["identity"]


@pytest.mark.plugins
def test_stock_meshtastic_is_read_by_its_survey_probe_and_entered_by_the_stock_cli():
    s = spec()
    assert s["tool"] == "probe_meshtastic.py" and s["fields"] == {"version": "firmware_version"}
    assert any("--enter-dfu" in t for t in familyspec.of("meshtastic")["uf2_enter"]["app_tools"])


@pytest.mark.plugins
def test_what_a_stock_board_runs_is_its_version_and_the_commit_it_ends_in(probe):
    ran, run, answer = probe
    got = common.read_by_tool(broker_client.NoBroker("board9"), "meshtastic", spec(), run=run)
    assert (got["version"], got["build"]) == ("2.7.26.54e0d8d", "54e0d8d")
    assert ran[0][0][-1] == "/dev/ttyACM3" and ran[0][1] == 60, "the guarded port, with a deadline"
    assert _Lock.seen == [("/dev/ttyACM3", "rxrbench-probe-meshtastic")]


@pytest.mark.plugins
def test_a_port_another_holds_is_not_asked(probe):
    ran, run, answer = probe
    _Lock.held = False
    with pytest.raises(common.NotOurs, match="held"):
        common.read_by_tool(broker_client.NoBroker("board9"), "meshtastic", spec(), run=run)
    assert ran == []


@pytest.mark.plugins
def test_a_board_that_does_not_answer_the_probe_or_is_not_there_is_not_ours(probe):
    ran, run, answer = probe
    answer[0] = (124, "(killed after 60 s)")
    with pytest.raises(common.NotOurs, match="does not answer as meshtastic"):
        common.read_by_tool(broker_client.NoBroker("board9"), "meshtastic", spec(), run=run)
    with pytest.raises(common.NotOurs, match="cannot resolve"):
        common.read_by_tool(broker_client.NoBroker("other"), "meshtastic", spec(), run=run)


def test_a_board_with_no_broker_has_nothing_to_release_and_no_console():
    b = broker_client.NoBroker("board9")
    assert b.release(ttl=5)["ok"] and b.reacquire()["ok"]
    with pytest.raises(broker_client.BrokerError, match="no broker"):
        b.exec("meshtastic state")


def stock_image(rig):  # noqa: F811
    img, _m, tools, tmp = rig
    m = {"family": "meshtastic", "format": "uf2", "file": "app.uf2", "board_tag": "xiao",
         "version": "2.7.26.54e0d8d", "build": "54e0d8d", "hash": "cd" * 32}
    (img / "app.uf2").write_bytes(b"\0" * 512)
    tool = script(tmp / "uf2_flash", 'echo "$@" >> "$0.args"; echo "programmed; app up"\n')
    return m, str(img / "app.uf2"), tool, tools


def flasher(board, m, path, tool, tools):
    return uf2.Uf2Flasher(board, "board9", m, path, board_tag="xiao",
                          declared={"family": "meshtastic", "image": "x"},
                          uf2_tool=tool, devreg=tools["devreg"], settle=1.0)


@pytest.mark.plugins
def test_a_stock_board_that_runs_the_release_is_left_alone(rig, monkeypatch):  # noqa: F811
    m, path, tool, tools = stock_image(rig)
    monkeypatch.setattr(common, "read_by_tool", lambda b, fam, s: identity.make(
        "meshtastic", version="2.7.26.54e0d8d", build="54e0d8d"))
    r = common.drive(flasher(broker_client.NoBroker("board9"), m, path, tool, tools), m)
    assert r["changed"] is False


@pytest.mark.plugins
def test_a_console_board_going_to_stock_is_a_family_switch(rig, monkeypatch):  # noqa: F811
    """The probe cannot have the port while the old firmware's broker holds it; the board
    answers as zephyrtastic through its broker, and the switch is an application copy."""
    m, path, tool, tools = stock_image(rig)

    def busy(b, fam, s):
        raise common.NotOurs("board9: its port is held (rxrbench-broker): not asked as meshtastic")
    monkeypatch.setattr(common, "read_by_tool", busy)
    f = flasher(FakeBoard(cls=1), m, path, tool, tools)
    r = common.drive(f, m, check_mode=True)
    assert r["changed"] and r["switch"] and r["why"] == "family switch: zephyrtastic -> meshtastic"


@pytest.mark.plugins
def test_uf2_mode_is_asked_of_the_firmware_the_board_runs_not_only_the_images(rig):  # noqa: F811
    m, path, tool, tools = stock_image(rig)
    import json
    f = flasher(broker_client.NoBroker("board9"), m, path, tool, tools)
    f.running_family = "zephyrtastic"                      # what drive() read it as, before the switch
    env = f._env()
    assert [e["family"] for e in json.loads(env["UF2_ENTERS"])] == ["zephyrtastic"]
    assert json.loads(env["UF2_ENTERS"])[0]["shell_command"] == "meshtastic dfu", \
        "zephyrtastic's shell, for a switch from it"
    assert env["UF2_RUNS"] == "zephyrtastic"


class _Broker:
    """A broker on the port: it holds the device lock until it releases the console."""
    def __init__(self):
        self.board, self.log = "board9", []

    def release(self, ttl=None):
        self.log.append("release")
        _Lock.held = True
        return "/dev/ttyACM3"

    def reacquire(self):
        self.log.append("reacquire")
        return {"ok": True}


@pytest.mark.plugins
def test_a_broker_on_the_port_lets_go_for_the_probe_and_takes_it_back(probe):
    """A board declared as a console family but running stock firmware (the way back from a
    switch): the probe must get the port, or the board is taken for unknown firmware."""
    ran, run, answer = probe
    _Lock.held = False
    b = _Broker()
    got = common.read_by_tool(b, "meshtastic", spec(), run=run)
    assert got["build"] == "54e0d8d" and b.log == ["release", "reacquire"]
    answer[0] = (1, "no phone API")
    with pytest.raises(common.NotOurs):
        common.read_by_tool(b, "meshtastic", spec(), run=run)
    assert b.log[-1] == "reacquire", "taken back even when the probe fails"


class _Serial:
    """A port: what the firmware answers to anything written to it."""
    def __init__(self, answer):
        self.answer, self.sent = answer, b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def reset_input_buffer(self):
        pass

    def write(self, b):
        self.sent += b

    def read(self, n):
        out, self.answer = self.answer, b""
        return out


def zt_line(build="abc12345", cls=1):
    import binascii
    j = json.dumps({"v": 1, "build": build, "image": "0.4.47+45", "class": cls, "board": "xiao_ble",
                    "feat": [], "id": "0x0a0b0c0d", "managed": 0}, separators=(",", ":"))
    return f"meshtastic state\r\n~S{j}*{binascii.crc_hqx(j.encode(), 0):04x}\r\nuart:~$ ".encode()


def console(probe, answer):
    ports = []

    def opener(path):
        ports.append(path)
        return _Serial(answer)
    return common.PortConsole("board9", serial_open=opener), ports


@pytest.mark.plugins
def test_a_board_with_no_broker_is_asked_on_its_port_like_the_survey_does(probe):
    b, ports = console(probe, zt_line())
    got = common.read_board(b, "zephyrtastic")
    assert (got["family"], got["build"], got["class"]) == ("zephyrtastic", "abc12345", 1)
    assert ports == ["/dev/ttyACM3"], "the guarded port"
    assert _Lock.seen[-1] == ("/dev/ttyACM3", "rxrbench-console")


@pytest.mark.plugins
def test_a_port_that_answers_nothing_is_not_that_family(probe):
    b, _ = console(probe, b"")
    with pytest.raises(common.NotOurs, match="no echo"):
        common.read_board(b, "zephyrtastic")


def test_a_port_another_holds_is_not_opened(probe):
    _Lock.held = False
    b, ports = console(probe, zt_line())
    with pytest.raises(broker_client.BrokerError, match="port busy"):
        b.exec("meshtastic state")
    assert ports == []


@pytest.mark.plugins
def test_going_to_stock_after_the_broker_was_retired_is_a_family_switch(rig, probe, monkeypatch):  # noqa: F811
    """The documented order (declare, hosts-setup, apply): the broker is gone before the flash,
    and the board, still on zephyrtastic, is read on its port, not taken for unknown firmware."""
    m, path, tool, tools = stock_image(rig)
    monkeypatch.setattr(common, "read_by_tool", lambda b, fam, s: (_ for _ in ()).throw(
        common.NotOurs("board9 does not answer as meshtastic")))
    b, _ = console(probe, zt_line())
    r = common.drive(flasher(b, m, path, tool, tools), m, check_mode=True)
    assert r["switch"] and r["why"] == "family switch: zephyrtastic -> meshtastic"
