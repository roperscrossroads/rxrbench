"""The launcher_apply module as a play runs it, against a fake console behind a real broker:
a plan that resets nothing, a plan of what differs, and an apply that streams an app through
the host tool on the released port, re-enters Launcher, reads the board back and records
what it installed."""
import hashlib
import json

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from ansible_collections.rxrbench.launcher.plugins.modules import launcher_apply
from tests.support import fake_module
from tests.support.launcher_rig import TOOL, Rig, resets_power_on
from tests.unit.test_launcher_webui import esp_image


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


@pytest.fixture
def declared(tmp_path):
    """One app, cam-stream, staged in tmp_path; the block wants it in slot camstr and booting."""
    data = esp_image(b"\x33" * 5000)
    f = tmp_path / "cam-stream.bin"
    f.write_bytes(data)
    rec = {"path": str(f), "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
           "app_offset": 0, "app_size": len(data), "image": "cam-stream", "version": None}
    return {"data": data, "record": rec,
            "params": {"board": "cyd", "images": {"manager": None, "apps": {"camstr": rec}},
                       "store": str(tmp_path), "launcher": {"apps": {"camstr": {"image": "cam-stream"}}, "boot": "camstr"},
                       "tools": {"serial_tool": TOOL, "devreg": "/nonexistent/devreg.py"}}}


def test_a_plan_of_a_board_in_its_app_is_deferred_and_resets_nothing(rig, declared, monkeypatch):
    node, sock = rig.make()
    node.state = "app"
    how, r = fake_module.run(launcher_apply, monkeypatch, {**declared["params"], "socket": sock}, check_mode=True)
    assert how == "exit" and r["changed"] is True and "a plan does not reset it" in r["deferred"]
    assert r["steps"] == [] and node.reboots == 0 and "nav SelPress" not in node.seen and node.state == "app"


def test_a_plan_of_a_board_in_launcher_says_what_differs_and_writes_nothing(rig, declared, monkeypatch):
    node, sock = rig.make()
    how, r = fake_module.run(launcher_apply, monkeypatch, {**declared["params"], "socket": sock}, check_mode=True)
    assert how == "exit" and r["changed"] is True
    n = len(declared["data"])
    assert r["steps"] == [f"install cam-stream into slot camstr ({n} B): not installed"] and r["needs_webui"] == []
    assert r["diff"]["before"]["apps"] == {} and r["diff"]["after"]["apps"]["camstr"]["image"] == "cam-stream"
    assert r["diff"]["after"]["boot"] == "camstr"
    assert not any(k.startswith("_") for k in r["launcher"]["settings"])
    assert node.flashed == [] and node.state == "menu", "a board that was in Launcher stays there"


def test_a_board_as_declared_plans_nothing_when_its_record_matches(rig, declared, monkeypatch):
    node, sock = rig.make(apps=[("camstr", 0x100000)], boot="camstr")
    rec = declared["record"]
    how, r = fake_module.run(launcher_apply, monkeypatch, {
        **declared["params"], "socket": sock,
        "records": {"camstr": {"sha256": rec["sha256"], "size": rec["size"], "image": "cam-stream"}}}, check_mode=True)
    assert how == "exit" and r["changed"] is False and r["steps"] == [] and "diff" not in r
    assert node.flashed == []


def test_an_apply_streams_the_app_reenters_launcher_reads_back_and_records_it(rig, declared, monkeypatch):
    node, sock = rig.make(tick=0.05)
    monkeypatch.setattr(common, "guarded_port", lambda devreg, name: (node.path, None))
    resets_power_on(monkeypatch, node, window=2.0)
    how, r = fake_module.run(launcher_apply, monkeypatch, {**declared["params"], "socket": sock,
                                                           "apply_allowed": True, "wait": 8})
    assert how == "exit", r
    assert r["changed"] is True and r["needs_webui"] == []
    assert node.flashed == [("camstr", declared["data"])], "the host tool streamed the app on the released port"
    rec = declared["record"]
    assert r["installed"] == {"camstr": {"sha256": rec["sha256"], "size": rec["size"], "image": "cam-stream"}}
    assert r["launcher"]["boot"] == "camstr" and [a["label"] for a in r["launcher"]["apps"]] == ["camstr"]
    assert r["diff"]["after"]["apps"]["camstr"]["sha256"] == rec["sha256"]
    assert node.seen.count("nav SelPress") == 1, "Launcher was entered again after the install rebooted the board"
    assert node.state == "app" and node.reboots == 2, "the install rebooted once, `leave` once more"


def test_an_apply_without_the_layouts_word_runs_nothing(rig, declared, monkeypatch):
    node, sock = rig.make()
    how, r = fake_module.run(launcher_apply, monkeypatch, {**declared["params"], "socket": sock})
    assert how == "fail" and "writing is not allowed" in r["msg"]
    assert node.seen == [] and node.flashed == []


def test_a_network_is_added_with_its_declared_password_which_never_shows(rig, declared, monkeypatch):
    node, sock = rig.make()
    params = {**declared["params"], "socket": sock, "apply_allowed": True,
              "images": {"manager": None, "apps": {}}, "launcher": {"wifi": [{"ssid": "new-net"}]},
              "secrets": {"wifi_psk": {"new-net": "s3cr3t-psk"}}}
    how, r = fake_module.run(launcher_apply, monkeypatch, params)
    assert how == "exit" and r["changed"] is True and r["steps"] == ["wifi add new-net"]
    assert {"ssid": "new-net", "pwd": "s3cr3t-psk"} in node.settings["wifi"]
    assert "s3cr3t-psk" not in json.dumps(r)
    assert r["launcher"]["settings"]["wifi"] == ["lab-iot", "new-net"]


def test_a_network_declared_without_a_password_is_a_failure_before_the_console(rig, declared, monkeypatch):
    node, sock = rig.make()
    params = {**declared["params"], "socket": sock, "apply_allowed": True,
              "images": {"manager": None, "apps": {}}, "launcher": {"wifi": [{"ssid": "new-net"}]}}
    how, r = fake_module.run(launcher_apply, monkeypatch, params)
    assert how == "fail" and r["msg"] == "cyd: no password declared for network new-net (launcher_secrets)"
    assert not any(s.startswith("wifi add") for s in node.seen)


def test_settings_and_the_webui_password_are_written_and_read_back(rig, declared, monkeypatch):
    node, sock = rig.make()
    params = {**declared["params"], "socket": sock, "apply_allowed": True,
              "images": {"manager": None, "apps": {}},
              "launcher": {"boot_timer": 2, "boot_to_app": False}, "secrets": {"wui_password": "n3w-pw"}}
    how, r = fake_module.run(launcher_apply, monkeypatch, params)
    assert how == "exit" and r["changed"] is True
    assert r["steps"] == ["settings bootTimer=2, bootToApp=false", "WebUI password -> as declared"]
    assert node.settings["bootTimer"] == 2 and node.settings["bootToApp"] is False and node.settings["wui_pwd"] == "n3w-pw"
    assert "n3w-pw" not in json.dumps(r)
    assert r["launcher"]["settings"]["boot_timer"] == 2 and r["launcher"]["settings"]["boot_to_app"] is False


# ---- the runner's install step, apart --------------------------------------------------------

class Port:
    calls = []

    def release(self, ttl=120.0):
        Port.calls.append("release")
        return "/dev/ttyFAKE"

    def reacquire(self):
        Port.calls.append("reacquire")


def _runner(monkeypatch, rc, out):
    Port.calls = []
    monkeypatch.setattr(common, "guarded_port", lambda devreg, name: ("/dev/ttyFAKE", None))
    monkeypatch.setattr(common, "tool", lambda argv, timeout, env=None, cwd=None: (rc, out))
    return launcher_apply.Runner(None, Port(), {"board": "cyd", "tools": {}, "wait": 1, "declared": {}})


def test_what_the_host_tool_says_last_decides_and_the_port_always_goes_back(monkeypatch):
    r = _runner(monkeypatch, 1, "noise\n" + json.dumps({"ok": False, "reply": "ERR no space for app"}) + "\n")
    with pytest.raises(common.FlashError, match=r"installing camstr failed \(rc 1\): ERR no space for app"):
        r.install("camstr", "/x/cam.bin", 0, 10, overwrite=False)
    assert Port.calls == ["release", "reacquire"]
    r = _runner(monkeypatch, 0, "not json at all\n")
    with pytest.raises(common.FlashError, match="installing camstr failed"):
        r.install("camstr", "/x/cam.bin", 0, 10, overwrite=False)
    r = _runner(monkeypatch, 0, json.dumps({"ok": True, "sent": 10}) + "\n")
    r.install("camstr", "/x/cam.bin", 0, 10, overwrite=True)


def test_a_guard_that_refuses_the_port_is_the_failure(monkeypatch):
    Port.calls = []
    monkeypatch.setattr(common, "guarded_port", lambda devreg, name: (None, "devreg: 'cyd' is NOT PRESENT"))
    r = launcher_apply.Runner(None, Port(), {"board": "cyd", "tools": {}, "wait": 1, "declared": {}})
    with pytest.raises(common.FlashError, match="NOT PRESENT"):
        r.install("camstr", "/x/cam.bin", 0, 10, overwrite=False)
    assert Port.calls == ["release", "reacquire"]


def test_a_console_token_is_quoted_with_launchers_escapes():
    assert launcher_apply.quoted('a"b\\c d') == '"a\\"b\\\\c d"'


def test_on_launcher_2_9_1_an_app_is_reinstalled_by_deleting_its_slot_first(rig, declared, monkeypatch):
    """A 2.9.1 console has no `flash overwrite` (and its `flash firmware` for a
    label already there makes a second slot): the slot is deleted, then installed afresh."""
    node, sock = rig.make(apps=[("camstr", 0x100000)], boot="camstr", version="2.9.1",
                          has_overwrite=False, tick=0.05)
    monkeypatch.setattr(common, "guarded_port", lambda devreg, name: (node.path, None))
    resets_power_on(monkeypatch, node, window=2.0)
    how, r = fake_module.run(launcher_apply, monkeypatch, {**declared["params"], "socket": sock,
                                                           "apply_allowed": True, "wait": 8})
    assert how == "exit", r
    assert "partition delete camstr" in node.seen
    assert node.flashed == [("camstr", declared["data"])], "into a slot named camstr again, not a second one"
    assert [a["label"] for a in r["launcher"]["apps"]] == ["camstr"] and r["launcher"]["boot"] == "camstr"
