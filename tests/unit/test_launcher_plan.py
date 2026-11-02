"""The Launcher plugin's plan (declared state against what the board holds), its images, and the
serial install tool against a fake Launcher console."""
import importlib.util
import json
import os

import pytest

from ansible_collections.rxrbench.launcher.plugins.module_utils import images, plan
from tests.support.fake_launcher import FakeLauncher
from tests.unit import esptables
from tests.unit.test_launcher_webui import esp_image

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "launcher", "host", "bin",
                    "launcher_serial.py")


def facts(apps=(("camstr", 0x100000, True),), version="60a40c8", wifi=None, wui="launcher"):
    rows = [{"label": l, "subtype": f"ota_{i}", "offset": 0x160000 + i * 0x100000, "size": s, "boot": b}
            for i, (l, s, b) in enumerate(apps)]
    wifi = {"lab-iot": "hunter22"} if wifi is None else wifi
    return {"version": version, "manager": {"label": "test", "offset": 0x10000, "size": 0x150000},
            "apps": rows, "data": [], "free": [], "flash_size": 4 << 20, "free_total": 0,
            "boot": next((r["label"] for r in rows if r["boot"]), None),
            "settings": {"boot_to_app": True, "boot_timer": 4, "wui_user": "admin", "dev_mode": False,
                         "wifi": sorted(wifi), "_wui_password": wui, "_wifi_passwords": wifi}}


def app(label, image="cam-stream", sha="aa" * 32, size=900_000):
    return {label: {"image": image, "sha256": sha, "size": size, "path": f"/store/{image}.bin",
                    "app_offset": 0, "app_size": size}}


def test_a_board_as_declared_plans_nothing():
    declared = {"manager": {"version": "60a40c8", "image": {}}, "apps": app("camstr"), "boot": "camstr",
                "settings": {"boot_to_app": True, "wui_user": "admin"}, "wui_password": "launcher",
                "wifi": [{"ssid": "lab-iot", "psk": "hunter22"}]}
    p = plan.plan(declared, facts(), {"camstr": {"sha256": "aa" * 32, "size": 900_000, "image": "cam-stream"}})
    assert p["steps"] == [] and p["needs_webui"] == []


def test_the_steps_come_in_order_launcher_settings_networks_apps_with_the_boot_slot_last():
    declared = {"manager": {"version": "70b51d9", "image": {"path": "/store/L.bin"}},
                "apps": {**app("camstr"), **app("wifite", image="wifi-test", sha="bb" * 32)}, "boot": "camstr",
                "settings": {"boot_to_app": False, "boot_timer": 2}, "wui_password": "s3cret",
                "wifi": [{"ssid": "lab-iot", "psk": "other"}, {"ssid": "new", "psk": "x"}], "prune_wifi": True}
    f = facts(wifi={"lab-iot": "hunter22", "old": "y"})
    p = plan.plan(declared, f, {})
    kinds = [s["kind"] for s in p["steps"]]
    assert kinds == ["manager", "settings", "wui_password", "wifi_replace", "wifi_add", "wifi_del",
                     "install", "install"]
    assert p["steps"][1]["changes"] == {"bootToApp": 0, "bootTimer": 2}
    installs = [s for s in p["steps"] if s["kind"] == "install"]
    assert [s["label"] for s in installs] == ["wifite", "camstr"], "the boot slot is installed last"
    assert installs[1]["overwrite"] is True and "no record" in installs[1]["why"]
    assert installs[0]["overwrite"] is False and installs[0]["why"] == "not installed"
    assert p["needs_webui"] == [], "the last install selects the boot slot"
    assert p["after"]["version"] == "70b51d9" and p["after"]["boot"] == "camstr"
    shown = [plan.shown(s) for s in p["steps"]]
    assert "s3cret" not in " ".join(shown) and "other" not in " ".join(shown)


def test_a_boot_slot_no_install_chooses_needs_the_webui():
    declared = {"manager": None, "apps": {**app("camstr"), **app("wifite", image="wifi-test", sha="bb" * 32)},
                "boot": "wifite", "settings": {}, "wifi": []}
    f = facts(apps=(("camstr", 0x100000, True), ("wifite", 0x100000, False)))
    rec = {"camstr": {"sha256": "aa" * 32}, "wifite": {"sha256": "bb" * 32}}
    p = plan.plan(declared, f, rec)
    assert p["steps"] == [] and plan.shown(p["needs_webui"][0]) == "boot slot camstr -> wifite"


def test_a_slot_whose_record_names_another_image_is_written_again_and_pruning_is_opt_in():
    declared = {"manager": None, "apps": app("camstr", sha="cc" * 32), "boot": None, "settings": {}, "wifi": []}
    f = facts(apps=(("camstr", 0x100000, True), ("extra", 0x100000, False)))
    p = plan.plan(declared, f, {"camstr": {"sha256": "aa" * 32, "image": "cam-stream-old"}})
    assert [s["kind"] for s in p["steps"]] == ["install"] and "cam-stream-old" in p["steps"][0]["why"]
    p = plan.plan({**declared, "prune": True}, f, {"camstr": {"sha256": "cc" * 32}})
    assert [(s["kind"], s["label"]) for s in p["steps"]] == [("prune", "extra")]


def test_the_declaration_is_built_from_the_images_the_block_and_the_secrets():
    imgs = {"manager": {"path": "/cache/abc/Launcher-X.bin", "sha256": "11" * 32, "size": 10, "version": "60a40c8",
                        "app_offset": 0x10000, "app_size": 5, "image": "launcher-x"},
            "apps": {"camstr": {"path": "/x/cam.bin", "sha256": "22" * 32, "size": 7, "app_offset": 0,
                                "app_size": 7, "image": "cam-stream"}}}
    d = plan.declared_from(imgs, "/store/launcher",
                           {"apps": {"camstr": {"image": "cam-stream"}}, "boot": "camstr", "boot_to_app": True,
                            "wifi": [{"ssid": "lab-iot"}, {"ssid": "open"}]},
                           {"wui_password": "pw", "wifi_psk": {"lab-iot": "hunter22"}})
    assert d["manager"]["image"]["path"] == "/store/launcher/Launcher-X.bin"
    assert d["apps"]["camstr"]["path"].endswith("/launcher/cam.bin") and d["boot"] == "camstr"
    assert d["settings"] == {"boot_to_app": True} and d["wui_password"] == "pw"
    assert d["wifi"] == [{"ssid": "lab-iot", "psk": "hunter22"}, {"ssid": "open", "psk": None}]


# ---- images -----------------------------------------------------------------------------

def test_the_app_part_of_a_plain_and_of_a_merged_image(tmp_path):
    plain = esp_image(b"\x22" * 500)
    assert images.app_part(plain) == (0, len(plain))
    merged = esptables.flash_with_table(esptables.LAUNCHER_4MB) + plain + b"\xff" * 1000
    assert images.app_part(merged) == (0x10000, len(plain))
    f = tmp_path / "L.bin"
    f.write_bytes(merged)
    d = images.describe(str(f))
    assert d["manager"] is True and d["merged"] is True and d["app_size"] == len(plain)
    with pytest.raises(images.ImageError):
        images.app_part(b"\x00" * 100)
    assert images.slot_bytes(900_000) == 0xE0000


# ---- the serial install tool against a fake console ---------------------------------------

@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("launcher_serial", TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_an_app_is_streamed_installed_and_selected(tool, tmp_path, capsys):
    node = FakeLauncher()
    try:
        data = esp_image(b"\x33" * 5000)
        f = tmp_path / "cam.bin"
        f.write_bytes(data)
        rc = tool.main(["--port", node.path, "--name", "cam-stream", "--file", str(f), "--wait", "3"])
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc == 0 and out["ok"] and out["sent"] == len(data) and out["reply"].startswith("OK flashed")
        assert node.flashed == [("camstr", data)] and node.boot == "camstr", "labelled as Launcher labels"
        assert [r[0] for r in node.apps()] == ["camstr"]
    finally:
        node.close()


def test_the_app_part_is_cut_out_of_a_merged_file_and_a_slot_reused(tool, tmp_path, capsys):
    node = FakeLauncher(apps=[("buddy0", 0x100000)], boot="buddy0")     # the label Launcher made of "buddy"
    try:
        app = esp_image(b"\x44" * 3000)
        merged = esptables.flash_with_table(esptables.LAUNCHER_4MB) + app
        f = tmp_path / "merged.bin"
        f.write_bytes(merged)
        rc = tool.main(["--port", node.path, "--name", "buddy", "--file", str(f), "--offset", "0x10000",
                        "--length", str(len(app)), "--overwrite", "--wait", "3"])
        assert rc == 0
        assert node.flashed == [("buddy0", app)] and len(node.apps()) == 1, "the slot was reused"
    finally:
        node.close()


def test_a_board_in_its_boot_window_is_entered_through_the_banner(tool, tmp_path, capsys):
    node = FakeLauncher(window=3.0, tick=0.05)
    try:
        f = tmp_path / "a.bin"
        f.write_bytes(esp_image(b"\x55" * 100))
        rc = tool.main(["--port", node.path, "--name", "a", "--file", str(f), "--wait", "5"])
        assert rc == 0 and "nav SelPress" in node.seen
    finally:
        node.close()


def test_an_image_that_does_not_fit_is_refused_by_the_device_and_said(tool, tmp_path, capsys):
    node = FakeLauncher(flash_size=0x170000)
    try:
        f = tmp_path / "big.bin"
        f.write_bytes(esp_image(b"\x66" * 200_000))
        rc = tool.main(["--port", node.path, "--name", "big", "--file", str(f), "--wait", "3"])
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc == 1 and not out["ok"] and out["reply"].startswith("ERR no space")
    finally:
        node.close()


def test_a_board_whose_version_cannot_be_read_is_not_updated_blind():
    declared = {"manager": {"version": "2.9.1", "image": {"path": "/store/L.bin"}}, "apps": {}, "boot": None,
                "settings": {}, "wifi": []}
    p = plan.plan(declared, facts(version="unknown"), {})
    assert p["steps"] == [] and len(p["needs_webui"]) == 1
    assert "cannot be read" in plan.shown(p["needs_webui"][0]) and "2.9.1" in plan.shown(p["needs_webui"][0])


# ---- the plan's corners ------------------------------------------------------------------

def declared(**over):
    base = {"manager": None, "apps": {}, "boot": None, "prune": False, "settings": {},
            "wui_password": None, "wifi": [], "prune_wifi": False}
    base.update(over)
    return base


def test_a_network_whose_declared_password_differs_is_replaced_and_extra_ones_pruned_only_when_asked():
    f = facts(wifi={"lab-iot": "old-psk", "guest": "g"})
    pl = plan.plan(declared(wifi=[{"ssid": "lab-iot", "psk": "new-psk"}]), f)
    assert [plan.shown(s) for s in pl["steps"]] == ["wifi replace lab-iot"]
    assert pl["after"]["wifi"] == ["guest", "lab-iot"], "a replace keeps the set"
    pl = plan.plan(declared(wifi=[{"ssid": "lab-iot", "psk": "old-psk"}], prune_wifi=True), f)
    assert [plan.shown(s) for s in pl["steps"]] == ["wifi del guest"]
    assert pl["after"]["wifi"] == ["lab-iot"]
    pl = plan.plan(declared(wifi=[{"ssid": "lab-iot"}]), f)
    assert pl["steps"] == [], "a declared network with no password to compare is left as it is"


def test_launcher_itself_is_updated_first_and_shown_from_to():
    mgr = {"version": "beta-7777777", "image": {"path": "/s/L.bin", "app_offset": 0x10000, "app_size": 10}}
    f = facts(version="beta-60a40c8", wifi={})
    pl = plan.plan(declared(manager=mgr, settings={"boot_timer": 9}), f)
    assert [plan.shown(s) for s in pl["steps"]] == ["Launcher beta-60a40c8 -> beta-7777777", "settings bootTimer=9"]
    assert pl["after"]["version"] == "beta-7777777" and pl["before"]["version"] == "beta-60a40c8"
    same = plan.plan(declared(manager={**mgr, "version": "beta-60a40c8"}), f)
    assert same["steps"] == []


def test_every_step_kind_has_a_line_and_none_carries_a_secret():
    lines = [plan.shown(s) for s in [
        {"kind": "wui_password"}, {"kind": "prune", "label": "wifite"},
        {"kind": "boot", "label": "camstr", "from": "wifite"},
        {"kind": "version", "to": "beta-7777777"},
        {"kind": "install", "label": "camstr", "image": "cam-stream", "app_size": 5000, "overwrite": True,
         "why": "the slot holds wifi-test"},
        {"kind": "settings", "changes": {"bootToApp": 0, "wui_usr": "ops"}},
        {"kind": "mystery"}]]
    assert lines == ["WebUI password -> as declared", "delete slot wifite", "boot slot wifite -> camstr",
                     "Launcher's version cannot be read on this build (no `version` command before main); "
                     "beta-7777777 is declared: compare through the WebUI's /systeminfo",
                     "install cam-stream into slot camstr (5000 B, reusing the slot): the slot holds wifi-test",
                     "settings bootToApp=0, wui_usr=ops", "mystery"]


def test_pruning_removes_only_undeclared_slots_and_the_boot_slot_goes_last():
    f = facts(apps=[("camstr", 0x100000, False), ("wifite", 0xE0000, True), ("oldapp", 0x50000, False)])
    rec = {l: {"sha256": "aa" * 32, "size": 900_000, "image": l} for l in ("camstr", "wifite", "oldapp")}
    pl = plan.plan(declared(apps={**app("camstr", image="camstr"), **app("wifite", image="wifite", sha="bb" * 32)},
                            boot="wifite", prune=True), f, rec)
    assert [plan.shown(s)[:30] for s in pl["steps"]] == ["install wifite into slot wifit", "delete slot oldapp"]
    assert pl["needs_webui"] == [] and pl["after"]["boot"] == "wifite"
    assert set(pl["after"]["apps"]) == {"camstr", "wifite"}


@pytest.mark.parametrize("version, reuse", [("2.9.1", False), ("2.9.0", False), ("unknown", False),
                                            (None, False), ("2.9.2", True), ("60a40c8", True)])
def test_a_slot_is_reused_only_by_a_launcher_that_has_flash_overwrite(version, reuse):
    """Launcher 2.9.1 answers a reinstall of an app whose slot nothing recorded with `ERR unknown
    command` (`flash overwrite` came after 2.9.1): there the slot is deleted first."""
    declared = {"manager": None, "apps": app("camstr"), "boot": "camstr"}
    (step,) = [s for s in plan.plan(declared, facts(version=version), {})["steps"] if s["kind"] == "install"]
    assert step["overwrite"] is reuse and step["replace"] is (not reuse)
    assert ("deleted first" in plan.shown(step)) is (not reuse)


def test_a_board_behind_a_uart_bridge_is_sent_one_piece_at_a_time(tool, tmp_path, capsys):
    """Behind a CH340 (no flow control) a board still writing flash loses bytes sent meanwhile, so
    each piece waits for its ACK, however late."""
    node = FakeLauncher(busy=3.0)
    try:
        data = esp_image(b"\x55" * 5000)          # three pieces
        f = tmp_path / "w.bin"
        f.write_bytes(data)
        rc = tool.main(["--port", node.path, "--name", "wifite", "--file", str(f), "--wait", "3"])
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc == 0 and out["ok"], out
        assert node.dropped == 0 and node.flashed == [("wifite", data)]
        assert out["lost_acks"] == 0, "late ACKs are waited for, not taken for lost"
    finally:
        node.close()


def test_an_ack_that_never_comes_ends_the_install_saying_where(tool, tmp_path, capsys):
    node = FakeLauncher(busy=4.0)
    try:
        data = esp_image(b"\x66" * 3000)
        f = tmp_path / "w.bin"
        f.write_bytes(data)
        rc = tool.main(["--port", node.path, "--name", "wifite", "--file", str(f), "--wait", "3",
                        "--ack-wait", "2"])
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc != 0 and not out["ok"], out
        assert node.flashed == [], "nothing was installed from a stream that lost bytes"
    finally:
        node.close()
