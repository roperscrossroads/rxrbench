"""Real Launcher boards on this machine, read through their brokers (tests/hw/README.md): what
a board prints is parsed whole, and the state module answers as a play would. Nothing written."""
import json
import os
import re
import subprocess

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.launcher.plugins.module_utils import console, images
from ansible_collections.rxrbench.launcher.plugins.modules import launcher_state
from tests.support import fake_module

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOARDS = [b for b in os.environ.get("RXRBENCH_HW_BOARDS", "").split(",") if b]
ENTER = set(b for b in os.environ.get("RXRBENCH_HW_ENTER", "").split(",") if b)
#: How the banner is answered on the boards entered: `selpress` (default) or `settings`.
ENTRY = os.environ.get("RXRBENCH_HW_ENTRY", "selpress")
RECORD = bool(os.environ.get("RXRBENCH_HW_RECORD"))
MAC = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")

pytestmark = pytest.mark.skipif(not BOARDS, reason="no board named in RXRBENCH_HW_BOARDS")


def present(board):
    r = subprocess.run(["devreg", "resolve", board], capture_output=True, text=True, check=False)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


@pytest.fixture(params=BOARDS)
def board(request):
    ok, said = present(request.param)
    if not ok:
        pytest.skip(f"{request.param}: {said}")
    return request.param


def record(board, version, printouts):
    d = os.path.join(REPO, "tests", "fixtures", "launcher", f"{board}-{version}")
    os.makedirs(d, exist_ok=True)
    for name, text in printouts.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(text if text.endswith("\n") else text + "\n")


def scrubbed(settings_text):
    """The settings JSON with the password and the chip's MAC replaced: a fixture, not a record."""
    j = json.loads(settings_text)
    j.pop("wui_pwd", None)
    for w in j.get("wifi") or []:
        w.pop("pwd", None)
    j["wui_pwd"] = "example-only"
    j = {(re.sub(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", "02:00:00:00:00:01", k, flags=re.I)): v for k, v in j.items()}
    return json.dumps(j, separators=(",", ":"))


def test_the_board_is_read_whole_and_its_printouts_parse(board):
    with broker_client.Board(board, timeout=60) as b:
        v = console.probe(b, deadline=4.0)
        entered = False
        if not v:
            if board not in ENTER:
                pytest.skip(f"{board} is in its app; name it in RXRBENCH_HW_ENTER to reset it into Launcher")
            v, how = console.enter(b, wait=30, entry=ENTRY)
            entered = how == "entered"
        try:
            version_text = console.exec_line(b, "version", 8.0) if v != console.NO_VERSION else console.UNKNOWN_COMMAND
            table_text = console.exec_line(b, "partitions", 8.0)
            settings_text = console.exec_line(b, "settings get", 8.0)
            got = console.read(b, 8.0)
        finally:
            if entered:
                console.leave(b)
    # what every Launcher board must print, and what the parsers must make of it
    assert v and got["version"] == v
    t = console.parse_partitions(table_text)
    assert t["manager"] is not None and t["manager"]["offset"] == 0x10000, "Launcher lives at 0x10000"
    assert t["manager"]["subtype"] in ("test", "factory")
    for a in t["apps"]:
        assert a["label"] == images.slot_label(a["label"]) or len(a["label"]) <= 6, a
        assert a["subtype"].startswith("ota_")
    assert t["flash_size"] and t["flash_size"] % (1 << 20) == 0, table_text
    assert t["boot"] is None or t["boot"] in [a["label"] for a in t["apps"]]
    s = console.parse_settings(settings_text)
    assert isinstance(s["boot_to_app"], bool) and s["wui_user"] and isinstance(s["wifi"], list)
    assert s["_settings_mac"] and MAC.match(s["_settings_mac"]) and s["_settings_board"], "a saved config names its board"
    assert got["settings_mac"] == s["_settings_mac"] and got["settings_board"] == s["_settings_board"]
    if RECORD:
        record(board, v, {"version.txt": version_text, "partitions.txt": table_text,
                          "settings_get.txt": scrubbed(settings_text),
                          "README.md": f"# Launcher {v} on {board}, as its serial console printed it\n\n"
                                       f"Read through the board's broker; `settings_get.txt` with the WebUI password "
                                       f"replaced by `example-only` and the chip's MAC by an example MAC.\n"})


def test_the_state_module_answers_as_a_play_would(board, monkeypatch):
    how, r = fake_module.run(launcher_state, monkeypatch, {"board": board, "enter": board in ENTER, "entry": ENTRY, "wait": 30})
    assert how == "exit", r
    L = r["launcher"]
    if not L["running"]:
        pytest.skip(f"{board} is in its app and was not entered")
    assert L["version"] and L["manager"]["offset"] == 0x10000 and L["flash_size"]
    assert MAC.match(L["settings_mac"]) and L["settings_board"]
    assert not any(k.startswith("_") for k in L["settings"]) and "wui_pwd" not in L["settings"]
    assert L["wui_password_matches"] is None, "no password was declared to this read"
    if L["entered"]:
        assert L["left"], "the app is booted again after an entered read"
