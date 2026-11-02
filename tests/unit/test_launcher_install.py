"""The launcher_install module: the one write of a Launcher release at 0x0 on a chip that has
none, and everything it refuses before touching a device."""
import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from ansible_collections.rxrbench.launcher.plugins.module_utils import console
from ansible_collections.rxrbench.launcher.plugins.modules import launcher_install
from tests.support import fake_module
from tests.unit import esptables
from tests.unit.test_launcher_webui import esp_image

TAGS = ["secure-boot:off", "flash-encryption:off", "rom-download:on"]


def params(p):
    return {"first_flash": True, "facts_tags": list(TAGS), "image": "/x/L.bin",
            "app_manager_overwrite": False, **p}


def test_refusals_name_each_missing_go_ahead():
    assert launcher_install.refusals(params({}), {"manager": True}) == []
    out = launcher_install.refusals(params({"first_flash": False, "facts_tags": ["secure-boot:off"]}),
                                    {"manager": False})
    assert [o.split(":")[0] for o in out] == ["writing Launcher at 0x0 erases the chip", "the survey facts do not show flash-encryption",
                                              "/x/L.bin is not a Launcher release image (no app manager in its table)"]
    assert "flash-encryption:off, rom-download:on" in out[1]


def test_an_app_manager_the_survey_found_is_not_written_over_without_the_layouts_word():
    tags = TAGS + ["app-manager:test", "app-manager-apps:2"]
    (why,) = launcher_install.refusals(params({"facts_tags": tags}), {"manager": True})
    assert "an app manager in test on this chip (2 apps installed)" in why and "bench_app_manager_overwrite" in why
    assert launcher_install.refusals(params({"facts_tags": tags, "app_manager_overwrite": True}), {"manager": True}) == []


class FakeBoard:
    """A broker connection that only releases and reacquires the port."""
    calls = []

    def __init__(self, board, path=None, timeout=60.0, busy_wait=120.0):
        self.board = board

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def release(self, ttl=120.0):
        FakeBoard.calls.append("release")
        return "/dev/ttyFAKE"

    def reacquire(self):
        FakeBoard.calls.append("reacquire")
        return {"ok": True}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    img = tmp_path / "Launcher.bin"
    img.write_bytes(esptables.flash_with_table(esptables.LAUNCHER_4MB) + esp_image(b"\x31" * 500))
    FakeBoard.calls = []
    ran = []
    monkeypatch.setattr(broker_client, "Board", FakeBoard)
    monkeypatch.setattr(launcher_install.os, "access", lambda p, m: True)
    monkeypatch.setattr(common, "esp_port", lambda devreg, name, esptool, chip: ("/dev/ttyFAKE", None))
    monkeypatch.setattr(common, "read_manager", lambda esptool, chip, port, after="no-reset": None)
    monkeypatch.setattr(common, "tool", lambda argv, timeout, env=None, cwd=None: (ran.append(argv), (0, "Hash of data verified."))[1])
    monkeypatch.setattr(console, "enter", lambda b, wait=25.0, poll=1.0, entry="selpress": ("2.9.1", "entered"))
    base = {"board": "cyd1", "image": str(img), "version": "2.9.1", "chip": "esp32", "first_flash": True,
            "facts_tags": list(TAGS), "apply_allowed": True}
    return base, ran


def test_a_plan_says_what_the_apply_would_do_and_touches_nothing(rig, monkeypatch):
    base, ran = rig
    how, r = fake_module.run(launcher_install, monkeypatch, base, check_mode=True)
    assert how == "exit" and r["changed"] is True and "written whole at 0x0 after an erase" in r["why"]
    assert r["image"]["manager"] is True and "path" not in r["image"]
    assert ran == [] and FakeBoard.calls == []


def test_the_write_erases_then_writes_at_0x0_and_the_version_is_what_launcher_says_after(rig, monkeypatch):
    base, ran = rig
    how, r = fake_module.run(launcher_install, monkeypatch, base)
    assert how == "exit" and r["changed"] is True and r["version"] == "2.9.1"
    assert [a[-1] for a in ran] == ["erase-flash", base["image"]]
    assert ran[1][-2] == "0x0" and "--port" in ran[1] and ran[1][ran[1].index("--port") + 1] == "/dev/ttyFAKE"
    assert FakeBoard.calls == ["release", "reacquire"], "the port goes back to the broker before Launcher is asked"


def test_a_version_other_than_declared_after_the_write_is_a_failure_that_says_both(rig, monkeypatch):
    base, _ran = rig
    monkeypatch.setattr(console, "enter", lambda b, wait=25.0, poll=1.0, entry="selpress": ("2.9.2", "entered"))
    how, r = fake_module.run(launcher_install, monkeypatch, base)
    assert how == "fail" and r["msg"] == "Launcher answers `2.9.2` after the write, the layout declares 2.9.1"
    assert r["changed"] is True and r["version"] == "2.9.2"


def test_refused_before_any_device_is_touched(rig, monkeypatch):
    base, ran = rig
    how, r = fake_module.run(launcher_install, monkeypatch, {**base, "first_flash": False, "facts_tags": []})
    assert how == "fail" and r["msg"].startswith("refused: writing Launcher at 0x0 erases the chip")
    assert "the survey facts do not show secure-boot:off, flash-encryption:off, rom-download:on" in r["msg"]
    assert ran == [] and FakeBoard.calls == []


def test_a_manager_read_off_the_chip_refuses_the_write_after_the_release(rig, monkeypatch):
    base, ran = rig
    mgr = {"label": "test", "offset": 0x10000, "size": 0x150000, "subtype": "test", "apps": [{"label": "buddy"}], "data": []}
    monkeypatch.setattr(common, "read_manager", lambda esptool, chip, port, after="no-reset": mgr)
    how, r = fake_module.run(launcher_install, monkeypatch, base)
    assert how == "fail" and r["msg"].startswith("refused: the chip carries") and "would orphan every installed app" in r["msg"]
    assert ran == [] and FakeBoard.calls == ["release", "reacquire"]


def test_without_the_layouts_go_ahead_nothing_runs(rig, monkeypatch):
    base, ran = rig
    how, r = fake_module.run(launcher_install, monkeypatch, {**base, "apply_allowed": False})
    assert how == "fail" and "writing is not allowed" in r["msg"] and ran == []


def test_no_esptool_and_a_file_that_is_no_image_are_said(rig, monkeypatch, tmp_path):
    base, _ran = rig
    monkeypatch.setattr(launcher_install.os, "access", lambda p, m: False)
    how, r = fake_module.run(launcher_install, monkeypatch, base)
    assert how == "fail" and r["msg"].startswith("no esptool at ")
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"\x00" * 0x11000)
    how, r = fake_module.run(launcher_install, monkeypatch, {**base, "image": str(junk)})
    assert how == "fail" and r["msg"] == f"{junk}: not an ESP-IDF app image, nor a merged image with a table"
