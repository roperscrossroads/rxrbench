import importlib.util
import os
import threading
import time

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import meshcore, state
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from tests.support.fake_meshcore import FakeMeshCore

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("broker", os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "broker.py"))
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)


def test_the_password_fingerprint_is_the_firmwares():
    # what zephcore's native_sim repeater printed (`"pw_fp":"f5d6bda8"`) for its sim-only
    # password: this module computes the same fingerprint as the firmware
    assert meshcore.pw_fp("sim-only-pw-001") == "f5d6bda8"


def test_a_radio_answer_is_read_with_its_float_noise_rounded():
    assert meshcore.parse_radio("910.5250244,62.5,7,5") == {"freq_mhz": 910.525, "bw_khz": 62.5,
                                                            "sf": 7, "cr": 5}


def test_a_value_answer_and_an_error():
    assert meshcore.value("> ridge-1") == "ridge-1"
    with pytest.raises(meshcore.MeshCoreError):
        meshcore.value("Error: refused, that is a secret")


def test_the_state_line_is_checked():
    node = FakeMeshCore()
    try:
        line = node.state()
        f = meshcore.state_facts(line)
        assert f["state_supported"] and f["role"] == "repeater" and f["radio"]["tx_enabled"] is False
        bad = line[:-1] + ("0" if line[-1] != "0" else "1")
        with pytest.raises(state.StateError):
            meshcore.state_facts(bad)
        assert meshcore.state_facts("  -> Unknown command") is None
    finally:
        node.close()


@pytest.fixture
def broker_on(tmp_path):
    made = []

    def make(node):
        console = mb.Console("mc1", mb.path_opener(node.path), str(tmp_path / "mc1.log"))
        broker = mb.Broker(console, str(tmp_path / "run" / "mc1.sock"))
        t = threading.Thread(target=broker.run, daemon=True)
        t.start()
        made.append((node, broker, t))
        end = time.time() + 3
        while not console.is_open() and time.time() < end:
            time.sleep(0.01)
        return broker_client.Board("mc1", path=broker.sock_path, timeout=20)

    yield make
    for node, broker, t in made:
        broker.shutdown()
        t.join(timeout=2)
        node.close()


def test_a_zephcore_board_is_read_from_its_state_line(broker_on):
    node = FakeMeshCore()
    with broker_on(node) as b:
        f = meshcore.read(b, "zephcore", meshcore.STATE_COMMAND)
        ident = common.read_board(b, "zephcore")
    assert f["state_supported"] and f["storage"]["epoch"] == 0
    assert ident["family"] == "zephcore" and ident["role"] == "repeater" and ident["build"] == "abcdef01"


def test_stock_meshcore_is_read_from_single_answers(broker_on):
    node = FakeMeshCore(family="meshcore", state_line=False)
    with broker_on(node) as b:
        f = meshcore.read(b, "meshcore", None)
    assert f["state_supported"] is False
    assert f["name"] == "zephcore" and f["radio"]["sf"] == 7 and f["radio"]["tx_dbm"] == 10
    assert f["pubkey_fp"] == meshcore.key_fp(node.st["pubkey"]) and f["pw_fp"] is None
    rel = meshcore.legacy_facts({"ver": "> v1.17.1-d929643 (Build: 14-Aug-2026)"[2:]}, "meshcore")
    assert (rel["version"], rel["build"]) == ("v1.17.1", "d929643")


def test_stock_meshcore_says_what_it_runs_in_its_ver_answer(broker_on):
    node = FakeMeshCore(family="meshcore", state_line=False)
    with broker_on(node) as b:
        ident = common.read_board(b, "meshcore")
    assert ident["family"] == "meshcore" and ident["version"] == "v1.17.1"
    assert "zephcore state" not in node.seen, "a family with no state line is not asked for one"
    from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
    fallback = familyspec.of("meshcore")["identity"]["fallback"]
    # what a release build answers: its version, then the commit it was built from
    said = familyspec.fields_of_fallback(fallback, "v1.17.1-d929643 (Build: 14-Aug-2026)")
    assert said == {"version": "v1.17.1", "build": "d929643"}

# ---- plans and writes ----------------------------------------------------------------

RADIO = {"freq_mhz": 910.525, "bw_khz": 62.5, "sf": 7, "cr": 5, "tx_dbm": 10, "tx_enabled": False}


def facts(**radio):
    return {"name": "zephcore", "pw_fp": None, "radio": {**RADIO, **radio}}


def test_nothing_to_do_is_an_empty_plan():
    assert meshcore.radio_plan(dict(RADIO), facts()) == []
    assert meshcore.radio_plan({"freq_mhz": 910.5250244}, facts()) == []   # float noise


def test_the_transmitter_goes_off_first_and_on_last():
    plan = meshcore.radio_plan({"sf": 8, "tx_dbm": 2, "tx_enabled": True}, facts(tx_enabled=True))
    # the power after the reboot that applies the parameters: a reboot must not undo it
    assert plan == ["zephcore tx off", "set radio 910.525,62.5,8,5", meshcore.REBOOT, "set tx 2",
                    "zephcore tx on"]


def test_a_receive_only_node_is_turned_on_only_when_declared():
    assert meshcore.radio_plan({"tx_dbm": 2}, facts()) == ["set tx 2"]
    assert meshcore.radio_plan({"tx_dbm": 2, "tx_enabled": True}, facts()) == ["set tx 2",
                                                                             "zephcore tx on"]


def test_a_node_with_no_transmitter_switch_is_not_switched():
    """The stock CLI reads no `tx_enabled` (such a node transmits whenever it is powered): the
    declaration's is for the RF rules, and the plan neither turns it on nor off."""
    assert meshcore.radio_plan({"tx_dbm": 10, "tx_enabled": True}, facts(tx_enabled=None)) == []
    assert meshcore.radio_plan({"sf": 8, "tx_enabled": False}, facts(tx_enabled=None)) == [
        "set radio 910.525,62.5,8,5", meshcore.REBOOT]


def test_the_password_step_is_shown_redacted():
    plan = meshcore.config_plan({"admin_password": "rotate-me-123"}, facts(), {})
    assert plan == [("password rotate-me-123", "password <secret>")]
    assert meshcore.config_plan({"admin_password": "rotate-me-123"},
                                {**facts(), "pw_fp": meshcore.pw_fp("rotate-me-123")}, {}) == []


def test_a_layout_block_the_firmware_would_refuse_is_refused():
    p = meshcore.check("n1", {"name": "a,b", "advert": {"local_interval_min": 2,
                                                        "flood_interval_h": 47},
                              "admin_password": "short"})
    # the name (a comma MeshCore refuses), the 2-minute advert (0 or 60-240), the short password
    assert len(p) == 3


def test_radio_and_config_are_written_and_read_back(broker_on, tmp_path):
    node = FakeMeshCore()
    desired = {"freq_mhz": 914.875, "bw_khz": 125.0, "sf": 9, "cr": 5, "tx_dbm": 2,
               "tx_enabled": True}
    with broker_on(node) as b:
        f = meshcore.read(b, "zephcore", meshcore.STATE_COMMAND)
        steps = meshcore.run_steps(b, meshcore.radio_plan(desired, f), "zephcore", settle=10)
        conf = {"name": "ridge-1", "admin_password": "rotate-me-123",
                "advert": {"flood_interval_h": 12}}
        steps += meshcore.run_steps(b, meshcore.config_plan(conf, meshcore.read(b, "zephcore"),
                                                            {"flood.advert.interval": "47"}),
                                    "zephcore")
        after = meshcore.read(b, "zephcore")
    assert node.reboots == 1
    assert meshcore.radio_plan(desired, after) == []
    assert after["name"] == "ridge-1" and after["pw_fp"] == meshcore.pw_fp("rotate-me-123")
    assert node.st["flood_h"] == 12
    assert "rotate-me-123" not in " ".join(steps)
    assert "rotate-me-123" not in open(tmp_path / "mc1.log", "rb").read().decode(errors="replace")


# ---- storage epoch -------------------------------------------------------------------

def test_the_same_epoch_is_no_wipe_and_a_new_one_is():
    f = {"storage": {"epoch": 3}}
    assert meshcore.storage_plan({"epoch": 3}, f) is None
    assert meshcore.storage_plan({"epoch": 4}, f) == "zephcore wipe 4 keep"
    assert meshcore.storage_plan({"epoch": 4, "keep_identity": False}, f) == "zephcore wipe 4 new"
    with pytest.raises(meshcore.MeshCoreError):
        meshcore.storage_plan({"epoch": 0}, f)


def test_a_wipe_keeps_or_replaces_the_identity_as_declared(broker_on):
    node = FakeMeshCore()
    with broker_on(node) as b:
        before = meshcore.read(b, "zephcore")
        after = meshcore.wipe(b, meshcore.storage_plan({"epoch": 2}, before), "zephcore", settle=10)
        assert after["storage"]["epoch"] == 2 and after["pubkey_fp"] == before["pubkey_fp"]
        assert after["radio"]["tx_enabled"] is False            # a wiped node is receive-only
        assert meshcore.storage_plan({"epoch": 2}, after) is None
        new = meshcore.wipe(b, meshcore.storage_plan({"epoch": 3, "keep_identity": False}, after),
                            "zephcore", settle=10)
        assert new["pubkey_fp"] != after["pubkey_fp"]
    assert node.wipes == [(2, "keep"), (3, "new")]


# ---- the identity key (escrow) --------------------------------------------------------

def test_a_key_line_is_checked_before_it_is_believed():
    import base64
    import binascii
    key = bytes(range(64))
    b64 = base64.b64encode(key).decode()
    good = f"~K{b64}*{binascii.crc_hqx(b64.encode(), 0):04x}"
    assert meshcore.parse_key(good) == key
    with pytest.raises(meshcore.MeshCoreError, match="checksum"):
        meshcore.parse_key(good[:-1] + ("0" if good[-1] != "0" else "1"))
    with pytest.raises(meshcore.MeshCoreError, match="~K"):
        meshcore.parse_key("  -> Error: refused")


def test_a_key_goes_out_and_back_in_and_never_reaches_the_log(broker_on, tmp_path):
    node = FakeMeshCore()
    with broker_on(node) as b:
        first = meshcore.key_export(b)
        before = meshcore.read(b, "zephcore")["pubkey_fp"]
        node.set_key(bytes([0x33]) * 64)                # as a wipe `new` or a reflash would
        assert meshcore.read(b, "zephcore")["pubkey_fp"] != before
        after = meshcore.key_import(b, "zephcore", first)
        assert after["pubkey_fp"] == before and meshcore.key_export(b) == first
    log = (tmp_path / "mc1.log").read_bytes()
    assert first.hex().encode() not in log, "set prv.key is never echoed"
    import base64
    assert base64.b64encode(first)[:24] not in log, "a ~K line never reaches the log"
    assert b"~K<key, " in log


def test_a_stock_node_gives_its_key_as_hex_and_takes_it_back(broker_on, tmp_path):
    node = FakeMeshCore(family="meshcore", state_line=False)
    with broker_on(node) as b:
        first = meshcore.key_export(b, None)
        assert first == node.st["prv"]
        before = meshcore.read(b, "meshcore", None)["pubkey_fp"]
        node.set_key(bytes([0x44]) * 64)                # as `erase` or a reflash would
        assert meshcore.read(b, "meshcore", None)["pubkey_fp"] != before
        after = meshcore.key_import(b, "meshcore", first, None, settle=30, deadline=2)
        assert after["pubkey_fp"] == before and meshcore.key_export(b, None) == first
    log = (tmp_path / "mc1.log").read_bytes()
    assert first.hex().encode() not in log.lower(), "the `get prv.key` answer never reaches the log"
    assert b"<key, " in log


def test_a_stock_key_answer_that_is_not_a_key_is_refused_without_quoting_it():
    with pytest.raises(meshcore.MeshCoreError, match="did not answer with a key") as e:
        meshcore.parse_hex_key("> " + "ab" * 40 + "zz")
    assert "abab" not in str(e.value)
    with pytest.raises(meshcore.MeshCoreError, match="32 bytes"):
        meshcore.parse_hex_key("> " + "ab" * 32)


def test_a_password_from_the_secrets_is_checked_when_it_is_filled_in():
    assert meshcore.check("b", {"admin_password": "{{ zephcore_secrets.b.admin_password }}"}) == []
    assert meshcore.check("b", {"admin_password": "short"}), "a literal one still is"


def test_a_state_line_without_floats_gets_its_radio_from_get_radio():
    import binascii
    j = ('{"v":1,"family":"zephcore","version":"0.1.0","build":"b8b3df7d","board":"x",'
         '"board_tag":"heltec-v4","role":"repeater","pubkey":"aa","pubkey_fp":"0001",'
         '"name":"n","radio":{"freq_mhz":*float*,"bw_khz":*float*,"sf":7,"cr":5,"tx_dbm":10,'
         '"tx_enabled":false},"storage":{"fs":"ok","epoch":0},"uptime_s":1}')
    line = f"~S{j}*{binascii.crc_hqx(j.encode(), 0):04x}"

    class Board:
        def exec(self, cmd, deadline=None, dialect=None):
            return line if cmd == meshcore.STATE_COMMAND else "> 910.5250244,62.5,7,5"
    f = meshcore.read(Board(), "zephcore")
    assert (f["radio"]["freq_mhz"], f["radio"]["bw_khz"], f["radio"]["sf"]) == (910.525, 62.5, 7)



# ---- stock MeshCore: what an erase loses, put back --------------------------------------

def test_names_are_held_to_what_meshcore_takes():
    assert meshcore.check("b", {"name": "Test Repeater T114"}) == []
    for bad in ("", " lead", "trail ", "a,b", "x" * 32, "what?"):
        assert meshcore.check("b", {"name": bad}), bad


def test_prefs_are_planned_by_value_and_owned_keys_are_refused():
    gets = {"lat": "12.3456789", "lon": "-45.6789012", "txdelay": "0.5", "repeat": "on"}
    want = {"prefs": {"lat": 12.345679, "lon": -45.6789012, "txdelay": 0.5, "repeat": True}}
    assert meshcore.config_plan(want, {"state_supported": False}, gets) == []
    gets["txdelay"] = "1.0"
    assert meshcore.config_plan(want, {"state_supported": False}, gets) == [("set txdelay 0.5",) * 2]
    assert meshcore.check("b", {"prefs": {"tx": 22}}) and meshcore.check("b", {"prefs": {"prv.key": "x"}})
    assert meshcore.check("b", {"prefs": {"owner.info": "x", "direct.txdelay": 0.3}}) == []


def test_a_password_a_stock_node_cannot_show_is_written_only_with_a_restore():
    want = {"admin_password": "longenough1"}
    stock = {"state_supported": False}
    assert meshcore.config_plan(want, stock, {}) == [], "a node found as declared is left alone"
    assert [s[0] for s in meshcore.config_plan(want, stock, {}, restored=True)] == ["password longenough1"]
    both = meshcore.config_plan({**want, "name": "n1"}, {**stock, "name": "zephcore"}, {})
    assert [s[0] for s in both] == ["set name n1", "password longenough1"]
    assert all("longenough1" not in s[1] for s in both)


def test_a_stock_node_erased_comes_back_as_itself(broker_on):
    """The drill on the fake: escrow, erase, reboot (a new key, defaults), then the key back and
    the declared config, the password with it."""
    node = FakeMeshCore(family="meshcore", state_line=False)
    with broker_on(node) as b:
        key = meshcore.key_export(b, None)
        before = meshcore.read(b, "meshcore", None)["pubkey_fp"]
        node.st["prefs"]["lat"] = "12.3456789"
        assert b.exec("erase", dialect="meshcore").startswith("File system erase: OK")
        b.exec("reboot", deadline=2.0, dialect="meshcore", expect_reply=False)
        time.sleep(4.5)
        after_erase = meshcore.read(b, "meshcore", None, deadline=2)
        assert after_erase["pubkey_fp"] != before and node.st["prefs"]["lat"] == "0.0"
        back = meshcore.key_import(b, "meshcore", key, None, settle=30, deadline=2)
        assert back["pubkey_fp"] == before
        want = {"name": "Test Repeater", "prefs": {"lat": 12.3456789},
                "advert": {"flood_interval_h": 12}, "admin_password": "longenough1"}
        gets = {"lat": node.st["prefs"]["lat"], "flood.advert.interval": node.st["flood_h"]}
        plan = meshcore.config_plan(want, back, gets, restored=True)
        meshcore.run_steps(b, plan, "meshcore", None, settle=30, deadline=2)
    assert node.st["name"] == "Test Repeater" and node.st["prefs"]["lat"] == "12.3456789"
    assert node.st["password"] == "longenough1" and node.st["flood_h"] == 12


def test_a_stock_node_repeating_its_password_keeps_it_out_of_the_log(broker_on, tmp_path):
    node = FakeMeshCore(family="meshcore", state_line=False)
    with broker_on(node) as b:
        done = meshcore.run_steps(b, [("password longenough1", "password <secret>")], "meshcore",
                                  None, deadline=2)
    assert node.st["password"] == "longenough1" and "longenough1" not in " ".join(done)
    assert b"longenough1" not in (tmp_path / "mc1.log").read_bytes()



def test_a_stock_esp32_reboot_is_told_by_its_boot_banner(broker_on):
    """Stock MeshCore: the console stays open and the node answers at once; with no uptime, the
    reboot is still seen."""
    node = FakeMeshCore(family="meshcore", state_line=False, console_stays=True)
    with broker_on(node) as b:
        key = meshcore.key_export(b, None)
        node.set_key(bytes([0x55]) * 64)
        t = time.monotonic()
        back = meshcore.key_import(b, "meshcore", key, None, settle=20, deadline=2)
        assert back["pubkey_fp"] == meshcore.key_fp(node.st["pubkey"]) and node.st["prv"] == key
        assert time.monotonic() - t < 15 and node.reboots == 1
    assert meshcore.booted("meshcore", "x\r\nrst:0xc (RTC_SW_CPU_RST),boot:0x28\r\n")
    assert not meshcore.booted("meshcore", "  -> OK") and not meshcore.booted("zephcore", "rst:0xc (")


def test_a_scenario_or_device_test_step_reaches_a_meshcore_console_by_its_dialect(broker_on, monkeypatch):
    """device_run reads a MeshCore console's one-line answer, not a shell's read to its prompt."""
    import importlib
    dr = importlib.import_module("ansible_collections.rxrbench.core.plugins.modules.device_run")
    node = FakeMeshCore(family="meshcore", state_line=False)
    with broker_on(node) as b:
        sock = b.path

    class Mod:
        check_mode = False

        def __init__(self, dialect):
            self.params = {"commands": [{"name": "ver", "cmd": "ver", "expect": "^v1\\.17",
                                         "refuse": None, "rc": None, "timeout": None}],
                           "board": "mc1", "socket": sock, "route": None, "dialect": dialect,
                           "console_tail": 0, "read_only": True, "fail_on_failure": False,
                           "timeout": 3.0}
            self.result = None

        def exit_json(self, **kw):
            self.result = kw
            raise SystemExit(0)

        fail_json = exit_json
    for dialect, passed in (("meshcore", True), ("", False)):
        mod = Mod(dialect)
        monkeypatch.setattr(dr, "AnsibleModule", lambda **_kw: mod)
        with pytest.raises(SystemExit):
            dr.main()
        assert bool(mod.result["results"]) and mod.result["results"][0]["passed"] is passed, mod.result


@pytest.mark.parametrize("stays, seen", [(True, "banner"), (False, "the console going down")])
def test_just_reboot_tells_a_stock_node_rebooted_without_an_uptime(broker_on, stays, seen):
    """core reboot takes a family that cannot say its uptime."""
    from ansible_collections.rxrbench.core.plugins.module_utils import reboot
    node = FakeMeshCore(family="meshcore", state_line=False, console_stays=stays)
    with broker_on(node) as b:
        r = reboot.reboot(b, "meshcore", settle=20, deadline=2)
    assert node.reboots == 1 and r["seen_by"] == seen and r["before_s"] is None


def test_a_family_with_no_uptime_and_no_ready_command_is_still_not_rebooted():
    from ansible_collections.rxrbench.core.plugins.module_utils import familyspec, reboot
    spec = familyspec.of("launcher")["reboot"]
    assert spec["uptime"] is None and not spec.get("ready")
    with pytest.raises(reboot.RebootError, match="does not say how long"):
        reboot.commands("launcher")



def test_a_late_answer_to_the_clearing_blank_line_is_never_taken_for_the_commands(broker_on):
    """Stock MeshCore answers the broker's blank line with "-> Unknown command"; a late one is
    drained, not read as the answer to the command after it."""
    node = FakeMeshCore(family="meshcore", state_line=False)
    node.blank_delay, node.answer_delay = 0.25, 0.4    # the stray lands before the answer
    with broker_on(node) as b:
        for _ in range(3):
            assert b.exec("get tx", dialect="meshcore", deadline=3) == "> 10"


def test_a_console_that_does_not_answer_a_blank_line_is_not_slowed_after_the_first_command(broker_on):
    node = FakeMeshCore(family="meshcore", state_line=False)   # no answer to a blank line
    with broker_on(node) as b:
        b.exec("ver", dialect="meshcore", deadline=3)          # learns it, waiting 1 s once
        t = time.monotonic()
        for _ in range(5):
            b.exec("ver", dialect="meshcore", deadline=3)
        assert time.monotonic() - t < 2.5



def test_a_multi_line_answer_comes_back_whole(broker_on):
    """`neighbors` prints one line per neighbour in one answer, and the dialect keeps them all."""
    node = FakeMeshCore(family="meshcore", state_line=False)
    node.neighbours = ["3EF7C6DB:379635:51", "25AC66B0:1307755:14", "63CE91B9:1:45"]
    with broker_on(node) as b:
        assert b.exec("neighbors", dialect="meshcore", deadline=3).split("\n") == node.neighbours
        assert b.exec("get tx", dialect="meshcore", deadline=3) == "> 10", "one-line answers as before"
