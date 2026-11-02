import json

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import region_flow

ESP = {"family": "zephcore", "format": "esp-parts"}
REG = [("fs", 0x430000, 0x70000)]


def image(tmp_path, family, fmap):
    (tmp_path / "manifest.json").write_text(json.dumps({"family": family}))
    (tmp_path / "flash.json").write_text(json.dumps({"map": fmap}))
    return str(tmp_path)


@pytest.mark.plugins
def test_zephcore_settings_are_its_file_system(tmp_path):
    d = image(tmp_path, "zephcore", {"ZEPHCORE_FS": "0x430000", "ZEPHCORE_FS_SIZE": "0x70000",
                                     "STORAGE": "0x420000", "STORAGE_SIZE": "0x8000"})
    assert region_flow.regions_of(d) == [("fs", 0x430000, 0x70000)]


def test_an_answering_board_on_this_image_may_be_erased():
    assert region_flow.erase_refusals(ESP, REG, "fs", True, True, False) == []


def test_a_silent_board_needs_wedged_declared():
    assert region_flow.erase_refusals(ESP, REG, "fs", False, False, False)
    assert region_flow.erase_refusals(ESP, REG, "fs", False, False, True) == []


def test_a_board_running_another_image_is_refused():
    assert "may not be the board's" in region_flow.erase_refusals(ESP, REG, "fs", True, False, True)[0]


def test_only_a_settings_region_and_only_an_esp32():
    assert "never erased here" in region_flow.erase_refusals(ESP, REG, "slot0", True, True, False)[0]
    uf2_board = {"family": "zephcore", "format": "uf2"}
    assert "has no USB erase path" in region_flow.erase_refusals(uf2_board, REG, "fs", True, True, False)[0]
