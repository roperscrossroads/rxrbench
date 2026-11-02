"""A board's flash as regions, and the rule a write must pass (docs/ARCHITECTURE.md#partitions-and-the-write-rule)."""
import struct

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import esppart, regions

APP = b"\xe9\x04\x02\x20" + struct.pack("<I", 0x40080000) + b"\0" * 56
MCUBOOT = bytes.fromhex("3db8f396") + b"\0" * 60
ERASED = b"\xff" * 64


def row(label, ptype, sub, off, size):
    return (esppart.MAGIC + bytes([ptype, sub]) + struct.pack("<II", off, size)
            + label.encode().ljust(16, b"\0") + b"\0" * 4)


# a chip with an app manager in `test` and two installed apps, and room for a third
TABLE = b"".join([row("nvs", 1, 2, 0x9000, 0x5000), row("otadata", 1, 0, 0xE000, 0x2000),
                  row("test", 0, 0x20, 0x10000, 0x150000), row("app1", 0, 0x10, 0x160000, 0x100000),
                  row("app2", 0, 0x11, 0x260000, 0x100000), row("app3", 0, 0x12, 0x360000, 0x100000)])
FLASH = {0x10000: APP, 0x160000: APP, 0x260000: MCUBOOT, 0x360000: ERASED}
LAYOUT = regions.from_esp_table(esppart.parse(TABLE))


def read_counting(log):
    def read(off, n):
        log.append(off)
        return FLASH.get(off, b"\0" * n)[:n]
    return read


def test_recognisers_say_an_image_is_there_never_whose():
    assert regions.recognise(APP) == "esp-app-image"
    assert regions.recognise(MCUBOOT) == "mcuboot-image"
    assert regions.recognise(ERASED) is None and regions.erased(ERASED)
    # the magic alone is not an image: an ESP32 entry point must be in the chip's address space
    assert regions.recognise(b"\xe9" + b"\0" * 63) is None


def test_a_plugin_adds_a_recogniser_and_core_keeps_its_own():
    regions.register_recognisers([{"name": "widget-image", "at": 0, "magic": "57494447"}])
    try:
        assert regions.recognise(b"WIDG" + b"\0" * 60) == "widget-image"
        assert regions.recognise(APP) == "esp-app-image"
    finally:
        regions.register_recognisers([])


def test_full_reads_every_app_region_and_names_owners():
    log = []
    inv = regions.inventory(LAYOUT, read_counting(log), declared=[(0x160000, 0x100000)])
    by = {r["label"]: r for r in inv}
    assert sorted(log) == [0x10000, 0x160000, 0x260000, 0x360000]
    assert by["app1"]["owner"] == "declared" and by["test"]["owner"] == "foreign"
    assert by["app2"]["owner"] == "foreign" and by["app2"]["evidence"]["recogniser"] == "mcuboot-image"
    assert by["app3"]["owner"] == "empty" and by["nvs"]["owner"] == "declared"
    shared = regions.inventory(LAYOUT, read_counting([]), declared=[], shared_data=True)
    assert {r["label"]: r["owner"] for r in shared}["nvs"] == "shared"


def test_spot_reads_only_the_regions_the_write_covers():
    log = []
    writes = [(0x160000, 0x80000)]
    inv = regions.inventory(LAYOUT, read_counting(log), declared=writes, depth="spot", writes=writes)
    assert log == [0x160000]
    assert {r["label"]: r["evidence"].get("depth") for r in inv if r["kind"] == "app"} == \
        {"test": "spot", "app1": "spot", "app2": "spot", "app3": "spot"}


@pytest.mark.parametrize("depth", ["full", "spot"])
def test_the_safety_decision_is_the_same_at_both_depths(depth):
    # a whole image from 0x0 that reaches into app1, with a new table that drops app2
    writes = [(0x0, 0x8000), (0x8000, 0xC00), (0x10000, 0x200000)]
    new = [r for r in LAYOUT if r["label"] != "app2"]
    drops = regions.dropped(LAYOUT, new)
    inv = regions.inventory(LAYOUT, read_counting([]), declared=[(0x10000, 0x100000)],
                            depth=depth, writes=writes + drops)
    why = regions.refusals(inv, writes, drops=drops)
    assert any("write covers app1" in w and "does not declare" in w for w in why)
    assert any("table drops app2" in w for w in why)
    assert not any(" test at" in w for w in why), "the declared image's own region is the layout's"


def test_a_table_that_drops_only_empty_or_declared_regions_passes():
    new = [r for r in LAYOUT if r["label"] != "app3"]       # app3 is empty
    drops = regions.dropped(LAYOUT, new)
    assert drops == [(0x360000, 0x100000)]
    inv = regions.inventory(LAYOUT, read_counting([]), declared=[], writes=drops)
    assert regions.refusals(inv, [], drops=drops) == []


def test_a_declared_overwrite_lets_the_write_take_those_regions():
    writes = [(0x160000, 0x100000)]
    inv = regions.inventory(LAYOUT, read_counting([]), declared=[])
    assert regions.refusals(inv, writes) != []
    assert regions.refusals(inv, writes, allowed=("app1",)) == []
    assert regions.refusals(inv, writes, allowed=("all",)) == []


def test_a_write_into_an_empty_or_declared_region_passes_and_one_outside_every_region_does_not():
    inv = regions.inventory(LAYOUT, read_counting([]), declared=[(0x160000, 0x100000)])
    assert regions.refusals(inv, [(0x160000, 0x1000), (0x360000, 0x1000), (0x0, 0x8000)]) == []
    assert "outside every region" in regions.refusals(inv, [(0x900000, 0x1000)])[0]


def test_the_loader_takes_a_plugins_recognisers(widgets):
    """The fixture plugin exports one (RECOGNISERS): loaded, core recognises its images."""
    assert regions.recognise(b"WIDG" + b"\0" * 60) == "widget-image"


def test_a_recogniser_goes_with_its_plugin():
    """With no plugin loaded, a plugin's images are not recognised."""
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families
    try:
        families.load([])
        assert regions.recognise(b"WIDG" + b"\0" * 60) is None
    finally:
        families.load()


def _otadata(*entries):
    """otadata bytes holding (seq, state) entries at 0 and 0x1000, the way ESP-IDF writes them."""
    import binascii
    blob = bytearray(b"\xff" * 0x2000)
    for i, (seq, state) in enumerate(entries):
        crc = binascii.crc32(struct.pack("<I", seq), 0xFFFFFFFF) & 0xFFFFFFFF
        blob[i * 0x1000:i * 0x1000 + 32] = (struct.pack("<I", seq) + b"\xff" * 20
                                            + struct.pack("<II", state, crc))
    return bytes(blob)


def test_the_boot_app_is_the_ota_slot_otadata_selects_else_factory_else_first():
    rows = esppart.parse(TABLE)
    assert esppart.boot_app(rows, None)["label"] == "test"   # no otadata: no factory app, so the first app
    assert esppart.boot_app(rows, _otadata((2, 2)))["label"] == "app2"   # seq 2 -> ota_1
    assert esppart.boot_app(rows, _otadata((1, 2), (3, 2)))["label"] == "app3"  # highest seq wins
    assert esppart.boot_app(rows, _otadata((1, 2), (3, 3)))["label"] == "app1"  # an INVALID entry is not counted
    bad = bytearray(_otadata((2, 2)))
    bad[28] ^= 0xFF                                                        # a broken CRC is not counted
    assert esppart.boot_app(rows, bytes(bad))["label"] == "test"


def _chip_reads(monkeypatch, blob):
    from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
    monkeypatch.setattr(common, "read_flash", lambda _e, _c, _p, off, n, **_k: blob[off:off + n])
    return common


def test_on_a_chip_with_a_manager_its_data_regions_are_shared_and_kept(monkeypatch):
    """A Launcher-like chip running a mesh app in ota_0: erasing that app's config in nvs
    or spiffs would take every app's (docs/ARCHITECTURE.md#partitions-and-the-write-rule)."""
    from tests.unit import esptables
    rows = list(esptables.MANAGED_4MB) + [esptables.row(0, 0x10, 0x160000, 0x100000, "mesh"),
                                           esptables.row(1, 0x82, 0x260000, 0x100000, "spiffs")]
    common = _chip_reads(monkeypatch, esptables.chip(rows, images=[0x10000, 0x160000], boot_seq=1))
    why = common.esp_write_refusals("esptool", "esp32", "/dev/x", [(0x9000, 0x5000)], what="erasing nvs")
    assert why == ["erasing nvs: the write covers nvs at 0x9000, which holds data every app on this "
                   "chip shares (it has an app manager); name it in bench_overwrite if that is meant"]
    assert common.esp_write_refusals("esptool", "esp32", "/dev/x", [(0x9000, 0x5000)],
                                     allowed=("nvs",)) == []
    assert "spiffs" in common.esp_write_refusals("esptool", "esp32", "/dev/x", [(0x260000, 0x100000)],
                                                 allowed=("nvs",))[0]


def test_on_a_plain_chip_the_data_regions_are_the_firmwares(monkeypatch):
    from tests.unit import esptables
    rows = [esptables.row(1, 0x02, 0x9000, 0x5000, "nvs"), esptables.row(1, 0x00, 0xE000, 0x2000, "otadata"),
            esptables.row(0, 0x10, 0x10000, 0x140000, "app0"), esptables.row(0, 0x11, 0x150000, 0x140000, "app1"),
            esptables.row(1, 0x82, 0x290000, 0x170000, "spiffs")]
    common = _chip_reads(monkeypatch, esptables.chip(rows, images=[0x10000, 0x150000], boot_seq=1))
    assert common.esp_write_refusals("esptool", "esp32", "/dev/x", [(0x9000, 0x5000), (0x290000, 0x170000)]) == []


# ---- ranges declared for a chip with no layout of its own (docs/ARCHITECTURE.md#partitions-and-the-write-rule)

def test_declared_ranges_are_read_and_their_mistakes_refused():
    ok, why = regions.declared([
        {"label": "boot", "kind": "boot", "start": "0x0", "size": "0x4000"},
        {"label": "app", "kind": "app", "start": 0x4000, "size": "0x1000"},
        {"label": "cfg", "kind": "data", "start": "0x5000", "size": "0x1000", "role": "config"}])
    assert why == [] and [r["start"] for r in ok] == [0, 0x4000, 0x5000] and ok[2]["role"] == "config"
    _, why = regions.declared([
        {"label": "a", "kind": "app", "start": "0x0", "size": "0x2000"},
        {"label": "b", "kind": "app", "start": "0x1000", "size": "0x1000"},
        {"label": "c", "kind": "rom", "start": "0x3000", "size": "0x10"},
        {"label": "d", "kind": "app", "start": "0x4000", "size": "0x10", "role": "config"},
        {"label": "e", "kind": "data", "start": "zero", "size": "0x10"}])
    text = "; ".join(why)
    for part in ("a and b overlap at 0x1000", "kind 'rom'", "config role is on a data region",
                 "e: start and size must be numbers"):
        assert part in text, text


def test_a_write_over_a_protected_boot_region_is_refused_unless_named():
    inv = [{"start": 0, "size": 0x4000, "label": "bootloader", "kind": "boot", "owner": "unknown"},
           {"start": 0x4000, "size": 0x1000, "label": "app", "kind": "app", "owner": "empty"}]
    (why,) = regions.refusals(inv, [(0, 0x5000)], protect=("boot",))
    assert "covers bootloader at 0x0" in why and "bench_overwrite" in why
    assert regions.refusals(inv, [(0, 0x5000)], allowed=("bootloader",), protect=("boot",)) == []
    assert regions.refusals(inv, [(0, 0x5000)]) == [], "unprotected unless the caller asks"
    assert regions.refusals(inv, [(0x4000, 0x100)], protect=("boot",)) == []
