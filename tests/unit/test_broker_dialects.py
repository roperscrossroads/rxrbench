"""A console dialect and its log rules as a file: what a plugin deploys beside the broker."""
import importlib.util
import json
import os
import threading
import time

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from tests.support.fake_meshcore import FakeMeshCore

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("broker_dialects",
                                              os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "broker.py"))
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)

# The one-line console of the built-in dialect, written as a plugin would write it.
ONELINE = {
    "reply": r"(?:^|\n)(?:  -> ([^\r\n]*)|(~S\{[^\r\n]*\}\*[0-9a-f]{4}))\r?\n",
    "secret_prefixes": ["password "],
    "log_scrub": [{"pattern": "~K(?P<s>[A-Za-z0-9+/]{8,}={0,2})", "label": "key"},
                  {"pattern": "token (?P<s>[0-9a-f]{8,})"}],
}


@pytest.fixture
def rules(tmp_path):
    d = tmp_path / "dialects.d"
    d.mkdir()
    (d / "oneline.json").write_text(json.dumps(ONELINE))
    (d / "notes.txt").write_text("not a dialect")
    before, extra = dict(mb.DIALECTS), list(mb.SCRUBS)
    mb.load_dialects(str(d))
    yield
    mb.DIALECTS.clear()
    mb.DIALECTS.update(before)
    mb.SCRUBS[:] = extra


@pytest.fixture
def board(tmp_path, rules):
    node = FakeMeshCore()
    log = str(tmp_path / "b.log")
    console = mb.Console("b", mb.path_opener(node.path), log)
    broker = mb.Broker(console, str(tmp_path / "run" / "b.sock"))
    t = threading.Thread(target=broker.run, daemon=True)
    t.start()
    end = time.time() + 3
    while not console.is_open() and time.time() < end:
        time.sleep(0.01)
    yield broker_client.Board("b", path=broker.sock_path, timeout=20), log
    broker.shutdown()
    t.join(timeout=2)
    node.close()


def test_a_dialect_from_a_file_reads_a_one_line_console(board):
    b, _log = board
    with b:
        assert b.exec("get name", dialect="oneline") == "> zephcore"
        assert b.exec("foo", dialect="oneline") == "Unknown command"


def test_a_files_secret_prefix_keeps_the_secret_out_of_the_log(board):
    b, log = board
    with b:
        assert b.exec("password correct-horse-9", dialect="oneline") == "OK"
    assert b"correct-horse-9" not in open(log, "rb").read()


def test_a_dialect_the_broker_has_no_rules_for_is_refused_by_name(board):
    b, _log = board
    with b, pytest.raises(broker_client.BrokerError, match="unknown dialect 'nosuch'.*oneline"):
        b.exec("get name", dialect="nosuch")


def test_a_files_log_rules_hold_for_every_line(rules):
    assert mb.scrub(b"~KQUJDREVGR0hJSktM*1a2b") == b"~K<key, 16 chars, not logged>*1a2b"
    assert mb.scrub(b"set token 0123456789abcdef ok") == b"set token <16 chars, not logged> ok"
    assert mb.scrub(b"an ordinary line") == b"an ordinary line"


def test_a_log_rule_must_say_what_to_hide(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "bad.json").write_text(json.dumps({"log_scrub": [{"pattern": "secret \\S+"}]}))
    with pytest.raises(ValueError, match="group named s"):
        mb.load_dialects(str(d))


# ---- a console that echoes nothing, with a boot banner the broker answers --------------

LAUNCHER_RULES = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "launcher",
                              "host", "dialects.d")


@pytest.fixture
def launcher_rules():
    before, extra = dict(mb.DIALECTS), list(mb.SCRUBS)
    mb.load_dialects(LAUNCHER_RULES)
    yield
    mb.DIALECTS.clear()
    mb.DIALECTS.update(before)
    mb.SCRUBS[:] = extra


@pytest.fixture
def launcher(tmp_path, launcher_rules):
    from tests.support.fake_launcher import FakeLauncher
    made = []

    def make(**kw):
        node = FakeLauncher(**kw)
        log = str(tmp_path / "cyd.log")
        console = mb.Console("cyd", mb.path_opener(node.path), log)
        broker = mb.Broker(console, str(tmp_path / "run" / "cyd.sock"))
        t = threading.Thread(target=broker.run, daemon=True)
        t.start()
        made.append((node, broker, t))
        end = time.time() + 3
        while not console.is_open() and time.time() < end:
            time.sleep(0.01)
        return node, broker_client.Board("cyd", path=broker.sock_path, timeout=20), log, console

    yield make
    for node, broker, t in made:
        broker.shutdown()
        t.join(timeout=2)
        node.close()


def test_a_console_that_echoes_nothing_is_read_for_its_answer_alone(launcher):
    node, board, _log, _c = launcher(apps=[("camstr", 0x100000)], boot="camstr")
    with board as b:
        assert b.exec("version", dialect="launcher") == "Launcher beta-60a40c8"
        assert b.exec("nav NextPress", dialect="launcher") == "OK nav NextPress"
        assert b.exec("frobnicate", dialect="launcher").startswith("ERR unknown command")
        table = b.exec("partitions", dialect="launcher")
        assert table.startswith("== Partition table ==") and "<-- BOOT" in table
        assert table.rstrip().splitlines()[-1].startswith("Flash size: 4MB")
        settings = b.exec("settings get", dialect="launcher")
        assert settings.startswith("{") and '"wui_usr":"admin"' in settings
    assert node.seen == ["version", "nav NextPress", "frobnicate", "partitions", "settings get"]


def test_a_settings_write_and_the_passwords_the_board_prints_stay_out_of_the_log(launcher):
    node, board, log, _c = launcher()
    with board as b:
        assert b.exec('settings set {"wui_pwd":"correct-horse-9"}', dialect="launcher") == "OK settings loaded"
        assert b.exec("settings get", dialect="launcher").count("correct-horse-9") == 1
    assert node.settings["wui_pwd"] == "correct-horse-9"
    time.sleep(1.2)
    text = open(log, "rb").read().decode(errors="replace")
    assert "correct-horse-9" not in text and "hunter22" not in text, "neither the typed nor the printed one"
    assert "not logged>" in text, "the printed one is scrubbed by the dialect's log rule"


def test_while_attending_the_broker_answers_the_boot_banner_once(launcher):
    node, board, log, console = launcher(window=2.0, tick=0.05)
    with board as b:
        assert b.request(op="attend", dialect="launcher", ttl=10)["attending"] is True
        end = time.time() + 3
        while node.state != "menu" and time.time() < end:
            time.sleep(0.02)
        assert node.state == "menu", "the banner was answered and the menu entered"
        assert b.exec("version", dialect="launcher") == "Launcher beta-60a40c8"
        assert console.attend is None and console.attended == 1
    assert node.seen.count("nav SelPress") == 1, "answered once, not for every banner line"
    assert "answered `Press the button" in open(log, "rb").read().decode(errors="replace")


def test_an_attend_that_is_not_asked_for_leaves_the_board_to_boot_its_app(launcher):
    node, board, _log, _c = launcher(window=0.3, tick=0.05)
    time.sleep(0.6)
    assert node.state == "app" and "nav SelPress" not in node.seen
    with board as b, pytest.raises(broker_client.BrokerError, match="timeout"):
        b.exec("version", dialect="launcher", deadline=0.5)


def test_attending_expires_and_can_be_cancelled(launcher):
    node, board, _log, console = launcher()
    with board as b:
        assert b.request(op="attend", dialect="launcher", ttl=0.2)["attending"] is True
        time.sleep(0.4)
        node.power_on()
        node.window = 0.3
        node.window_until = time.monotonic() + 0.3
        time.sleep(0.5)
        assert node.state == "app" and console.attend is None, "the attend had expired"
        assert b.request(op="attend", dialect="launcher", ttl=10)["attending"] is True
        assert b.request(op="attend", dialect="launcher", ttl=0)["attending"] is False
        assert console.attend is None
        r = b.request(op="attend", dialect="oneline", ttl=5)
        assert not r["ok"] and "no prompts" in r["error"]


def test_a_reset_on_a_port_without_modem_lines_is_an_error_not_a_crash(launcher):
    _node, board, _log, _c = launcher()
    with board as b:
        r = b.request(op="reset")
        assert r["ok"] is False and "modem lines" in r["error"]


def test_an_attend_may_answer_the_prompt_with_a_line_of_the_clients_choosing(launcher):
    """A build that loses a key press (the ThinkNode M9: its input task clears the flag before
    the boot loop reads it) is kept in Launcher by turning bootToApp off during the window:
    the console is live then, the device merges and applies the setting, the window ends in
    the menu. The client names the answer; the dialect's own stays the default."""
    node, board, log, console = launcher(window=1.0, tick=0.05, selpress_lost=True)
    with board as b:
        r = b.request(op="attend", dialect="launcher", ttl=10, answer='settings set {"bootToApp":0}')
        assert r["attending"] is True
        end = time.time() + 3
        while node.state != "menu" and time.time() < end:
            time.sleep(0.02)
        assert node.state == "menu" and node.settings["bootToApp"] == 0
        assert b.exec("version", dialect="launcher") == "Launcher beta-60a40c8"
        assert console.attended == 1 and "nav SelPress" not in node.seen
        r = b.request(op="attend", dialect="launcher", ttl=10, answer="two\nlines")
        assert not r["ok"] and "one line" in r["error"]
    assert "answered `Press the button" in open(log, "rb").read().decode(errors="replace")


def test_a_lost_key_press_leaves_the_board_to_boot_its_app(launcher):
    node, board, _log, _c = launcher(window=0.6, tick=0.05, selpress_lost=True)
    with board as b:
        assert b.request(op="attend", dialect="launcher", ttl=10)["attending"] is True
        time.sleep(1.0)
    assert node.seen.count("nav SelPress") == 1 and node.state == "app"
