import json

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, region_flow


# Core's region level on the fixture plugin's widget family (two regions, the first essential).
pytestmark = pytest.mark.usefixtures("widgets")


def image(tmp_path, flash, family="widget"):
    (tmp_path / "manifest.json").write_text(json.dumps({"family": family}))
    (tmp_path / "flash.json").write_text(json.dumps(flash))
    return str(tmp_path)


def test_the_regions_come_from_the_build_s_own_map(tmp_path):
    d = image(tmp_path, {"map": {"STORAGE": "0x420000", "STORAGE_SIZE": "0x8000",
                                 "BULK": "0x430000", "BULK_SIZE": "0x70000",
                                 "SLOT0": "0x20000"}})
    assert region_flow.regions_of(d) == [("storage", 0x420000, 0x8000),
                                          ("bulk", 0x430000, 0x70000)]


@pytest.mark.parametrize("flash", [{}, {"partitions": {"storage": "0x420000", "storage_size": "0x8000"}},
                                   {"map": {"SLOT0": "0x20000"}},
                                   {"map": {"STORAGE": "0x420000"}},
                                   {"map": {"STORAGE": "0x420000", "STORAGE_SIZE": "0x0"}}])
def test_a_map_that_does_not_say_where_the_settings_are_is_refused(tmp_path, flash):
    # the refusal names the map, not a family that knows no regions
    with pytest.raises(common.FlashError, match="guessed offset|has no STORAGE partition"):
        region_flow.regions_of(image(tmp_path, flash))


def test_no_flash_json_is_refused(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"family": "widget"}))
    with pytest.raises(common.FlashError, match="guessed offset"):
        region_flow.regions_of(str(tmp_path))


def test_an_image_that_names_no_family_has_no_known_settings(tmp_path):
    with pytest.raises(common.FlashError, match="unnamed firmware"):
        region_flow.regions_of(str(tmp_path))
