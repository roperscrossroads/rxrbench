"""The ESP-IDF partition table parser and what it recognises: an app manager (a factory or test
app on a chip whose otadata boots others), by structure, never by one manager's labels or sizes
(docs/ARCHITECTURE.md#partitions-and-the-write-rule)."""
from ansible_collections.rxrbench.core.plugins.module_utils import esppart
from tests.unit.esptables import MANAGED_4MB, row, table


def test_rows_parse_and_end_at_the_md5_or_an_erased_row():
    rows = esppart.parse(table(MANAGED_4MB))
    assert [r["label"] for r in rows] == ["nvs", "otadata", "test"]
    assert rows[2] == {"type": 0, "subtype": 0x20, "offset": 0x10000, "size": 0x150000,
                       "label": "test", "flags": 0}
    with_md5 = table(MANAGED_4MB) + b"\xeb\xeb" + b"\0" * 30 + row(0, 0x10, 0x200000, 0x100000, "ghost")
    assert len(esppart.parse(with_md5)) == 3, "rows after the MD5 row are not rows"


def test_no_table_is_no_rows_never_an_error():
    assert esppart.parse(b"\xff" * 0xC00) == []
    assert esppart.parse(b"\xe9\x03" + b"\0" * 100) == [], "MCUboot's bytes at 0x8000"
    assert esppart.parse(b"") == []
    assert esppart.manager([]) is None


def test_a_shipped_table_is_an_app_manager_with_no_apps_yet():
    m = esppart.manager(esppart.parse(table(MANAGED_4MB)))
    assert m["label"] == "test" and m["offset"] == 0x10000 and m["size"] == 0x150000
    assert m["subtype"] == "test" and m["apps"] == [] and m["data"] == []
    assert esppart.describe(m) == "an app manager in test at 0x10000 with apps: none installed"


def test_installed_apps_and_their_data_are_listed_in_flash_order():
    rows = MANAGED_4MB + [row(0, 0x11, 0x260000, 0x100000, "wifite"),
                           row(0, 0x10, 0x160000, 0x100000, "camstr"),
                           row(1, 0x82, 0x360000, 0x80000, "spiffs")]
    m = esppart.manager(esppart.parse(table(rows)))
    assert [a["label"] for a in m["apps"]] == ["camstr", "wifite"]
    assert m["apps"][0]["subtype"] == "ota_0" and m["apps"][1]["subtype"] == "ota_1"
    assert m["data"] == [{"label": "spiffs", "subtype": "spiffs", "offset": 0x360000, "size": 0x80000}]
    assert "apps: camstr, wifite" in esppart.describe(m)


def test_the_8mb_and_the_factory_variants_are_managers_too():
    eight = [row(1, 0x02, 0x9000, 0x5000, "nvs"), row(1, 0x00, 0xE000, 0x2000, "otadata"),
             row(0, 0x20, 0x10000, 0x170000, "app0"), row(1, 0x03, 0x180000, 0x10000, "coredump")]
    m = esppart.manager(esppart.parse(table(eight)))
    assert m and m["label"] == "app0" and m["data"][0]["label"] == "coredump"
    p4 = [eight[0], eight[1], row(0, 0x00, 0x10000, 0x180000, "app0")]
    assert esppart.manager(esppart.parse(table(p4)))["subtype"] == "factory"


def test_an_ordinary_idf_table_is_not_one():
    """A stock Arduino/IDF app: nvs, otadata, two OTA slots, spiffs. Nothing in the test slot."""
    stock = [row(1, 0x02, 0x9000, 0x5000, "nvs"), row(1, 0x00, 0xE000, 0x2000, "otadata"),
             row(0, 0x10, 0x10000, 0x140000, "app0"), row(0, 0x11, 0x150000, 0x140000, "app1"),
             row(1, 0x82, 0x290000, 0x170000, "spiffs")]
    assert esppart.manager(esppart.parse(table(stock))) is None
    # a factory app with no otadata boots nothing else: one firmware, not a manager
    single = [row(1, 0x02, 0x9000, 0x6000, "nvs"), row(0, 0x00, 0x10000, 0x100000, "factory")]
    assert esppart.manager(esppart.parse(table(single))) is None


def test_a_manager_is_known_by_structure_whatever_its_labels_offsets_and_sizes():
    """Any manager, not one project's: another nvs, another place and size for its own app."""
    other = [row(1, 0x02, 0x9000, 0x6000, "nvs"), row(1, 0x00, 0xF000, 0x2000, "otadata"),
             row(0, 0x20, 0x20000, 0x90000, "menu"), row(0, 0x10, 0xB0000, 0x100000, "game")]
    m = esppart.manager(esppart.parse(table(other)))
    assert m["label"] == "menu" and [a["label"] for a in m["apps"]] == ["game"]


def test_the_firmware_a_chip_runs_is_its_boot_app_and_on_a_plain_ab_pair_both_slots():
    stock = [row(1, 0x00, 0xE000, 0x2000, "otadata"), row(0, 0x10, 0x10000, 0x140000, "app0"),
             row(0, 0x11, 0x150000, 0x140000, "app1")]
    assert [r["label"] for r in esppart.firmware_apps(esppart.parse(table(stock)), None)] == ["app0", "app1"]
    managed = MANAGED_4MB + [row(0, 0x10, 0x160000, 0x100000, "camstr")]
    assert [r["label"] for r in esppart.firmware_apps(esppart.parse(table(managed)), None)] == ["test"]
