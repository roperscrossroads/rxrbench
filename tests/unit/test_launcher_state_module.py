"""The two read modules as a play runs them: launcher_state through a fake console behind a
real broker (entering Launcher from a running app and leaving it again), launcher_facts
through the WebUI client."""
import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.launcher.plugins.module_utils import webui
from ansible_collections.rxrbench.launcher.plugins.modules import launcher_facts, launcher_state
from tests.support import fake_module
from tests.support.launcher_rig import Rig, resets_power_on


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def test_a_board_in_launcher_is_read_with_its_secrets_compared_and_never_returned(rig, monkeypatch):
    node, sock = rig.make(apps=[("camstr", 0x100000), ("wifite", 0xE0000)], boot="camstr")
    how, r = fake_module.run(launcher_state, monkeypatch, {
        "board": "cyd", "socket": sock, "wui_password": "launcher",
        "wifi_psks": {"lab-iot": "hunter22", "other": "nope"}})
    assert how == "exit" and r["changed"] is False
    L = r["launcher"]
    assert (L["running"], L["entered"], L["left"], L["version"]) == (True, False, False, "beta-60a40c8")
    assert [a["label"] for a in L["apps"]] == ["camstr", "wifite"] and L["boot"] == "camstr"
    assert L["manager"]["label"] == "test" and L["flash_size"] == 0x400000
    assert L["settings"]["wui_user"] == "admin" and L["settings"]["wifi"] == ["lab-iot"]
    assert not any(k.startswith("_") for k in L["settings"]) and "wui_pwd" not in L["settings"]
    assert L["wui_password_matches"] is True and L["wifi_psk_matches"] == {"lab-iot": True, "other": False}
    assert "hunter22" not in str(r), "a network's password never leaves the module"
    assert "_wui_password" not in L["settings"] and "_wifi_passwords" not in L["settings"]
    assert node.state == "menu", "a board that was in Launcher is left there"


def test_a_wrong_declared_password_is_a_verdict_not_a_failure(rig, monkeypatch):
    _node, sock = rig.make()
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": "cyd", "socket": sock, "wui_password": "other"})
    assert how == "exit" and r["launcher"]["wui_password_matches"] is False
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": "cyd", "socket": sock})
    assert how == "exit" and r["launcher"]["wui_password_matches"] is None and r["launcher"]["wifi_psk_matches"] == {}


def test_a_board_in_its_app_is_entered_read_and_its_app_booted_again(rig, monkeypatch):
    node, sock = rig.make(apps=[("camstr", 0x100000)], boot="camstr", tick=0.05)
    node.state = "app"
    resets_power_on(monkeypatch, node, window=2.0)
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": "cyd", "socket": sock, "wait": 8})
    assert how == "exit"
    L = r["launcher"]
    assert (L["running"], L["entered"], L["left"], L["boot"]) == (True, True, True, "camstr")
    assert node.seen.count("nav SelPress") == 1, "the banner is answered once"
    assert node.reboots == 1 and node.state == "app", "`reboot` booted the app again"


def test_without_leave_the_board_stays_in_launcher(rig, monkeypatch):
    node, sock = rig.make(tick=0.05)
    node.state = "app"
    resets_power_on(monkeypatch, node, window=2.0)
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": "cyd", "socket": sock, "wait": 8, "leave": False})
    assert how == "exit" and r["launcher"]["entered"] is True and r["launcher"]["left"] is False
    assert node.reboots == 0 and node.state == "menu"


def test_without_enter_a_board_in_its_app_is_reported_not_reset(rig, monkeypatch):
    node, sock = rig.make()
    node.state = "app"
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": "cyd", "socket": sock, "enter": False})
    assert how == "exit" and r["launcher"] == {"running": False, "entered": False, "left": False}
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": "cyd", "socket": sock, "enter": False, "require": True})
    assert how == "fail" and r["msg"] == "cyd: no Launcher console answers" and r["launcher"]["running"] is False
    assert node.reboots == 0 and "nav SelPress" not in node.seen


def test_no_broker_is_a_failure_that_is_not_an_unanswered_port(rig, monkeypatch, tmp_path):
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": "cyd", "socket": str(tmp_path / "none.sock")})
    assert how == "fail" and r["msg"].startswith("cyd: no broker at") and r["unanswered"] is False


def test_a_closed_port_is_reported_as_unanswered(monkeypatch):
    class Gone:
        def __init__(self, board, path=None, timeout=60.0, busy_wait=120.0):
            self.board = board

        def __enter__(self):
            raise broker_client.BrokerError("cyd: `version`: port closed")

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(broker_client, "Board", Gone)
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": "cyd"})
    assert how == "fail" and r["unanswered"] is True and r["launcher"]["running"] is False


# ---- launcher_facts: the WebUI route --------------------------------------------------------

def _webui(monkeypatch, facts=None, error=None):
    made = []

    class UI:
        def __init__(self, url, username, password, session):
            made.append((url, username, password, session))
            if error:
                raise webui.LauncherError(error)
    monkeypatch.setattr(webui, "WebUI", UI)
    monkeypatch.setattr(webui, "facts", lambda ui: facts)
    return made


def test_facts_through_the_webui_are_returned_as_read(monkeypatch):
    made = _webui(monkeypatch, {"launcher": {"running": True, "version": "2.9.1", "logged_in": True},
                                "apps": [{"label": "camstr"}]})
    how, r = fake_module.run(launcher_facts, monkeypatch, {"url": "http://192.0.2.40", "password": "pw", "session": "tok"})
    assert how == "exit" and r["changed"] is False and r["launcher"]["version"] == "2.9.1" and r["apps"] == [{"label": "camstr"}]
    assert made == [("http://192.0.2.40", "admin", "pw", "tok")]


def test_no_webui_is_a_fact_unless_required(monkeypatch):
    _webui(monkeypatch, {"launcher": {"running": False, "version": None, "logged_in": False}})
    how, r = fake_module.run(launcher_facts, monkeypatch, {"url": "http://192.0.2.40"})
    assert how == "exit" and r["launcher"]["running"] is False
    how, r = fake_module.run(launcher_facts, monkeypatch, {"url": "http://192.0.2.40", "require": True})
    assert how == "fail" and r["msg"] == "no Launcher WebUI answers at http://192.0.2.40" and r["launcher"]["running"] is False


def test_a_client_error_is_the_failure_message(monkeypatch):
    _webui(monkeypatch, error="auth: wrong password")
    how, r = fake_module.run(launcher_facts, monkeypatch, {"url": "http://192.0.2.40", "password": "x"})
    assert how == "fail" and r["msg"] == "auth: wrong password"


def test_a_board_that_loses_the_key_press_is_entered_by_its_settings_and_reads_as_it_was(rig, monkeypatch):
    """The ThinkNode M9 answers `nav SelPress` and drops it; `settings set {"bootToApp":0}`
    during the window holds Launcher in its menu. The setting goes back on once the window
    has closed, so the read shows the board as it was and `reboot` boots the app again."""
    node, sock = rig.make(apps=[("m9base", 0x3F0000)], boot="m9base", tick=0.05, selpress_lost=True)
    node.state = "app"
    resets_power_on(monkeypatch, node, window=1.0)
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": "cyd", "socket": sock, "wait": 8, "entry": "settings"})
    assert how == "exit", r
    L = r["launcher"]
    assert (L["running"], L["entered"], L["left"], L["boot"]) == (True, True, True, "m9base")
    assert L["settings"]["boot_to_app"] is True, "read as it was: the entry's change is undone before the read"
    assert node.settings["bootToApp"] == 1 and node.state == "app"
    assert node.seen.count('settings set {"bootToApp":0}') == 1 and "nav SelPress" not in node.seen


def test_the_key_press_entry_on_a_board_that_loses_it_is_a_named_failure(rig, monkeypatch):
    node, sock = rig.make(tick=0.05, selpress_lost=True)
    node.state = "app"
    resets_power_on(monkeypatch, node, window=0.5)
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": "cyd", "socket": sock, "wait": 3})
    assert how == "fail" and "no Launcher console answered" in r["msg"]
    assert node.seen.count("nav SelPress") == 1 and node.state == "app"


def test_an_unknown_entry_is_refused_by_the_module(rig, monkeypatch):
    _node, sock = rig.make()
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": "cyd", "socket": sock, "entry": "touch"})
    assert how == "fail" and "entry" in r["msg"]
