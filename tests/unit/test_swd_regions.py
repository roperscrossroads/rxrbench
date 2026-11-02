"""A chip behind a debug probe, its config regions declared for it (docs/ARCHITECTURE.md#chip-and-region-flows,
"Phase 3 in detail"): backed up, written back and erased through the region flow, against the
fake host tool of test_swd_chip_backup. The chip's unique id ties a backup to its chip; the
image that declares the ranges must be the one the chip holds."""
import json
import os
import sys

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import access, common, region_flow
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_swd_chip_backup import FAKE_TOOL, SIZE, UID, Module, call, script  # noqa: E402

BASE = 0x08000000
APP_AT, APP = 0x08004000, bytes(range(256)) * 4
CONFIG_AT, CONFIG_SIZE = 0x0800F000, 0x1000
DECLARED = [
    {"label": "bootloader", "kind": "boot", "start": BASE, "size": 0x4000},
    {"label": "app", "kind": "app", "start": APP_AT, "size": 0xB000},
    {"label": "config", "kind": "data", "start": CONFIG_AT, "size": CONFIG_SIZE, "role": "config"},
]


def _at(flash, addr, data):
    off = addr - BASE
    return flash[:off] + data + flash[off + len(data):]


@pytest.fixture
def pill(tmp_path, monkeypatch):
    state = tmp_path / "flash.bin"
    flash = _at(b"\xff" * SIZE, APP_AT, APP)
    state.write_bytes(_at(flash, CONFIG_AT, os.urandom(CONFIG_SIZE)))
    monkeypatch.setenv("CHIP_STATE", str(state))
    monkeypatch.setenv("CHIP_UID", UID)
    image = tmp_path / "image"
    image.mkdir()
    (image / "app.bin").write_bytes(APP)
    (image / "manifest.json").write_text(json.dumps(
        {"family": "bare", "format": "raw-bin", "file": "app.bin", "base": hex(APP_AT)}))
    tools = {"swd_chip": script(tmp_path / "swd_chip.py", FAKE_TOOL),
             "devreg": script(tmp_path / "devreg.py",
                              "import sys\nprint('1-1.3' if '--busid' in sys.argv else '0483:3748')\n")}

    def params(**kw):
        return {"board": "pill", "flasher": "swd", "dest": str(tmp_path / "backups"),
                "tools": tools, "image": str(image), "flash_regions": DECLARED,
                "apply_allowed": True, "backup": "latest", "region": None, "wedged": False,
                "route": {"probe": "probe1", "chip": "STM32F103C8", "uid": UID}, **kw}
    params.state = state
    params.log = tmp_path / "flash.bin.log"
    return params


def config_of(pill):
    off = CONFIG_AT - BASE
    return pill.state.read_bytes()[off:off + CONFIG_SIZE]


def writes(pill):
    return [x for x in pill.log.read_text().splitlines() if x.startswith(("write", "erase"))]


def test_the_declared_config_ranges_are_the_config_regions():
    assert region_flow.regions_of("/nowhere", declared=DECLARED) == [("config", CONFIG_AT, CONFIG_SIZE)]


def test_a_config_region_is_backed_up_and_written_back_to_its_chip(pill):
    before = config_of(pill)
    got = call(access.backup, Module(), pill())
    meta = json.load(open(os.path.join(got["path"], "meta.json")))
    assert meta["route"] == "swd" and meta["uid"] == UID
    assert [(r["name"], r["offset"], r["size"]) for r in meta["regions"]] == [
        ("config", hex(CONFIG_AT), CONFIG_SIZE)]
    pill.state.write_bytes(_at(pill.state.read_bytes(), CONFIG_AT, b"\x00" * CONFIG_SIZE))
    plan = call(access.restore, Module(check_mode=True), pill())
    assert plan["changed"] and writes(pill) == [], "a plan writes nothing"
    call(access.restore, Module(), pill())
    assert config_of(pill) == before
    assert pill.state.read_bytes()[APP_AT - BASE:APP_AT - BASE + len(APP)] == APP, "the app is untouched"
    again = call(access.restore, Module(), pill())
    assert again["changed"] is False and len(writes(pill)) == 1, "a region that holds it is not written"


def test_another_chips_backup_is_refused_before_anything_is_written(pill):
    got = call(access.backup, Module(), pill())
    meta_path = os.path.join(got["path"], "meta.json")
    meta = json.load(open(meta_path))
    meta["uid"] = "0a0b0c0d0e0f101112131415"
    json.dump(meta, open(meta_path, "w"))
    with pytest.raises(common.FlashError, match="read from chip 0a0b0c0d0e0f101112131415"):
        access.restore(Module(), pill())
    assert writes(pill) == []


def test_a_chip_that_does_not_hold_the_declaring_image_is_not_read(pill):
    pill.state.write_bytes(_at(pill.state.read_bytes(), APP_AT, b"\x00" * len(APP)))
    with pytest.raises(common.FlashError, match="does not hold this image"):
        access.backup(Module(), pill())
    assert not os.path.exists(os.path.join(pill()["dest"], "pill"))


def test_a_write_the_chip_does_not_hold_afterwards_is_a_failure(pill, monkeypatch):
    call(access.backup, Module(), pill())
    pill.state.write_bytes(_at(pill.state.read_bytes(), CONFIG_AT, b"\x00" * CONFIG_SIZE))
    monkeypatch.setenv("CHIP_BROKEN", "1")
    with pytest.raises(common.FlashError, match="does not hash to the backup"):
        access.restore(Module(), pill())


def test_an_erase_blanks_the_config_region_and_nothing_else(pill):
    app_before = pill.state.read_bytes()[:CONFIG_AT - BASE]
    plan = call(access.erase, Module(check_mode=True), pill(region="config"))
    assert plan["region"] == {"name": "config", "offset": hex(CONFIG_AT), "size": CONFIG_SIZE}
    assert writes(pill) == []
    call(access.erase, Module(), pill(region="config"))
    assert config_of(pill) == b"\xff" * CONFIG_SIZE
    assert pill.state.read_bytes()[:CONFIG_AT - BASE] == app_before
    assert writes(pill) == [f"erase {CONFIG_SIZE} --chip STM32F103C8 --probe stlink --location 1-1.3 --uid {UID} "
                            f"--at {CONFIG_AT}"]


def test_an_erase_of_a_chip_running_something_else_needs_wedged(pill):
    pill.state.write_bytes(_at(pill.state.read_bytes(), APP_AT, b"\x00" * len(APP)))
    with pytest.raises(common.FlashError, match="needs `wedged: true`"):
        access.erase(Module(), pill(region="config"))
    assert writes(pill) == []
    call(access.erase, Module(), pill(wedged=True, region="config"))
    assert config_of(pill) == b"\xff" * CONFIG_SIZE


def test_an_app_range_is_never_erased_here(pill):
    with pytest.raises(common.FlashError, match="not one of this firmware's config regions"):
        access.erase(Module(), pill(region="app"))


def test_a_write_needs_the_go_ahead(pill):
    call(access.backup, Module(), pill())
    pill.state.write_bytes(_at(pill.state.read_bytes(), CONFIG_AT, b"\x00" * CONFIG_SIZE))
    with pytest.raises(common.FlashError, match="bench_apply_allowed"):
        access.restore(Module(), pill(apply_allowed=False))
    assert writes(pill) == []

