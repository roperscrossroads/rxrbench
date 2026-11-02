import os

import pytest
import yaml

from ansible_collections.rxrbench.core.plugins.module_utils import hwsurvey
from ansible_collections.rxrbench.core.plugins.plugin_utils import families

BOARDS = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "catalog", "boards.yml")))["boards"]

HELTEC = {"usb_vid": "303a", "usb_pid": "1001", "serial": "02:00:00:00:00:10",
          "product": "USB JTAG/serial debug unit", "busid": "1-1.2"}
XIAO_APP = {"usb_vid": "2fe3", "usb_pid": "0004", "serial": "2A2B2C2D2E2F3031", "busid": "1-1.3"}
XIAO_BOOT = {**XIAO_APP, "usb_vid": "2886", "usb_pid": "0045"}
XIAO_STOCK = {**XIAO_APP, "usb_vid": "239a", "usb_pid": "810b"}
REGISTRY = {
    "board2": {"kind": "serial", "match": {
        "serial_by_id": "usb-Espressif_USB_JTAG_serial_debug_unit_02:00:00:00:00:10-if00"}},
    "board7": {"kind": "serial", "match": {"usb_serial": "2A2B2C2D2E2F3031"}},
    "bridge1": {"kind": "serial", "match": {"host": "bench-a", "serial_by_id":
             "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0"}},
    "stlink-v2": {"kind": "usb", "match": {"usb_vid": "0483", "usb_pid": "3748"}},
    "a-disk": {"kind": "block", "match": {"usb_serial": "2A2B2C2D2E2F3031"}},
}
ESPTOOL_OUT = """esptool v5.4.0
Connected to ESP32-S3 on /dev/ttyACM5:
Chip type:          ESP32-S3 (QFN56) (revision v0.2)
Features:           Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB (AP_1v8)
Crystal frequency:  40MHz
MAC:                02:00:00:00:00:24

Flash Memory Information:
=========================
Manufacturer: c8
Device: 4018
Detected flash size: 16MB
"""


WIDGET_BOOT = {"usb_vid": "f1d1", "usb_pid": "0001", "serial": "2A2B2C2D2E2F3031", "busid": "1-1.3"}
WIDGET_STOCK = {**WIDGET_BOOT, "usb_pid": "0002"}


def test_a_plugins_usb_ids_give_its_boards_a_mode_and_an_identity(widgets):
    assert hwsurvey.classify(HELTEC)["ids"] == {"mac": "02:00:00:00:00:10"}
    for mode, usb in (("app", XIAO_APP), ("bootloader", WIDGET_BOOT), ("stock-app", WIDGET_STOCK)):
        c = hwsurvey.classify(usb)
        assert c["mode"] == mode and c["ids"] == {"chip_id": "2A2B2C2D2E2F3031"}
    assert hwsurvey.classify(WIDGET_BOOT)["mcu"] == "wm1"


@pytest.mark.plugins
def test_the_usb_serial_already_is_the_identity_on_both_board_families():
    """The mesh plugin's USB ids for a XIAO in each mode."""
    assert hwsurvey.classify(HELTEC)["ids"] == {"mac": "02:00:00:00:00:10"}
    for mode, usb in (("app", XIAO_APP), ("bootloader", XIAO_BOOT), ("stock-app", XIAO_STOCK)):
        c = hwsurvey.classify(usb)
        assert c["mode"] == mode and c["ids"] == {"chip_id": "2A2B2C2D2E2F3031"}


def test_hubs_are_infrastructure_and_strangers_are_unknown():
    assert hwsurvey.classify({"usb_vid": "05e3", "usb_pid": "0610", "bDeviceClass": "09"})["infra"]
    assert hwsurvey.classify({"usb_vid": "1234", "usb_pid": "5678"})["kind"] == "unknown"


def test_a_bridge_on_its_factory_serial_is_told_to_get_one_of_its_own():
    """Every bridge of the model ships with it, so the board behind it cannot be found by it."""
    factory = hwsurvey.classify({"usb_vid": "10c4", "usb_pid": "ea60", "serial": "0001"})
    assert factory["kind"] == "uart-bridge" and "factory default" in factory["note"]
    assert "cp210x_serial" in factory["note"]
    own = hwsurvey.classify({"usb_vid": "10c4", "usb_pid": "ea60", "serial": "node3"})
    assert "factory" not in own["note"]


def test_a_xiao_matches_its_entry_in_every_mode_and_a_heltec_by_its_by_id_name():
    for usb in (XIAO_APP, XIAO_BOOT, XIAO_STOCK):
        assert hwsurvey.match_registry(usb, REGISTRY) == ["board7"]
    by_id = ["usb-Espressif_USB_JTAG_serial_debug_unit_02:00:00:00:00:10-if00"]
    assert hwsurvey.match_registry(HELTEC, REGISTRY, by_id) == ["board2"]
    assert hwsurvey.match_registry(HELTEC, REGISTRY, []) == []


def test_a_match_that_names_a_host_holds_only_there():
    cp = {"usb_vid": "10c4", "usb_pid": "ea60", "serial": "0001"}
    by_id = ["usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0"]
    assert hwsurvey.match_registry(cp, REGISTRY, by_id, "bench-a") == ["bridge1"]
    assert hwsurvey.match_registry(cp, REGISTRY, by_id, "bench-b") == []


def test_a_bare_vid_pid_entry_matches_every_such_device_which_is_the_registrys_ambiguity():
    probe = {"usb_vid": "0483", "usb_pid": "3748", "serial": ""}
    assert hwsurvey.match_registry(probe, REGISTRY) == ["stlink-v2"]
    two = {**REGISTRY, "stlink-b": {"kind": "usb", "match": {"usb_vid": "0483",
                                                             "usb_pid": "3748"}}}
    assert hwsurvey.match_registry(probe, two) == ["stlink-b", "stlink-v2"]


def test_esptool_5_output():
    e = hwsurvey.parse_esptool(ESPTOOL_OUT)
    assert e["chip"] == "ESP32-S3" and e["revision"] == "0.2"
    assert e["mac"] == "02:00:00:00:00:24" and e["flash_size"] == "16MB"
    assert hwsurvey.psram_mb(e["features"]) == 8


def test_info_uf2_of_a_factory_xiao(widgets):
    info = hwsurvey.parse_info_uf2(
        "UF2 Bootloader 0.6.1 lib/nrfx (v2.0.0) lib/tinyusb (0.10.1-293-gaf8e5a90)\n"
        "Model: Seeed XIAO nRF52840\nBoard-ID: Seeed_XIAO_nRF52840_Sense\n"
        "SoftDevice: S140 version 7.3.0\nDate: Nov 12 2021\n")
    assert info == {"version": "0.6.1", "model": "Seeed XIAO nRF52840",
                    "board_id": "Seeed_XIAO_nRF52840_Sense",
                    "softdevice": "S140 version 7.3.0", "date": "Nov 12 2021"}
    rec = {"class": hwsurvey.classify(WIDGET_BOOT), "probes": {"bootloader": info}}
    assert {"bootloader:uf2-0.6.1", "softdevice:s140-7.3.0", "mcu:wm1",
            "usb-mode:bootloader"} <= set(hwsurvey.tags(rec))


def test_an_esp32_deep_probe_gives_its_chip_memory_and_security_tags():
    e = {**hwsurvey.parse_esptool(ESPTOOL_OUT),
         "efuse": {"SECURE_BOOT_EN": False, "DIS_DOWNLOAD_MODE": False,
                   "SPI_BOOT_CRYPT_CNT": "Disable"}}
    t = hwsurvey.tags({"class": hwsurvey.classify(HELTEC), "probes": {"esptool": e}})
    assert {"mcu:esp32s3", "psram:8MB", "flash:16MB", "radio:ble", "radio:wifi",
            "chip-rev:v0.2", "secure-boot:off", "rom-download:on",
            "flash-encryption:off"} <= set(t)


def test_an_app_manager_in_the_partition_table_is_tagged_with_its_apps():
    e = {**hwsurvey.parse_esptool(ESPTOOL_OUT),
         "partitions": {"manager": {"label": "test", "offset": 0x10000, "size": 0x150000,
                                    "apps": [{"label": "camstr"}, {"label": "wifite"}]}}}
    t = hwsurvey.tags({"class": hwsurvey.classify(HELTEC), "probes": {"esptool": e}})
    assert {"app-manager:test", "app-manager-apps:2"} <= set(t)
    e["partitions"] = {"manager": None, "rows": []}
    assert not any(x.startswith("app-manager") for x in
                   hwsurvey.tags({"class": hwsurvey.classify(HELTEC), "probes": {"esptool": e}}))


def test_a_probe_that_only_failed_gives_no_tags():
    rec = {"class": hwsurvey.classify(HELTEC),
           "probes": {"meshtastic": {"last_error": "Timed out", "last_error_at": "x"}}}
    assert "fw:stock-meshtastic" not in hwsurvey.tags(rec)
    assert "usb-mode:app" not in hwsurvey.tags(rec)


def test_each_family_says_what_its_probe_found(widgets):
    w = {"board": "xiao_ble", "class": 3}
    t = hwsurvey.tags({"class": hwsurvey.classify(XIAO_APP), "probes": {"widget": w}},
                      BOARDS, families.survey_tags)
    assert {"fw:widget", "mcu:nrf52840", "class:3"} <= set(t)
    g = {"model": "GADGET_ONE", "version": "2.1"}
    t = hwsurvey.tags({"class": hwsurvey.classify(WIDGET_STOCK), "probes": {"gadget": g}},
                      family_tags=families.survey_tags)
    assert {"fw:gadget", "hw:GADGET_ONE", "mcu:wm1"} <= set(t) and "fw:widget" not in t


@pytest.mark.plugins
def test_our_firmware_and_stock_firmware_say_what_they_are():
    zt = {"node_id": "0x0a0b0c0d", "board": "xiao_ble", "class": 12, "head": True}
    t = hwsurvey.tags({"class": hwsurvey.classify(XIAO_APP), "probes": {"zephyrtastic": zt}}, BOARDS, families.survey_tags)
    assert {"fw:zephyrtastic", "mcu:nrf52840", "class:12", "role:head"} <= set(t)
    st = {"hw_model": "SEEED_XIAO_S3", "has_bluetooth": True, "firmware_version": "2.7.26"}
    t = hwsurvey.tags({"class": hwsurvey.classify(XIAO_STOCK), "probes": {"meshtastic": st}}, family_tags=families.survey_tags)
    assert {"fw:stock-meshtastic", "hw:SEEED_XIAO_S3", "radio:ble"} <= set(t)


@pytest.mark.plugins
def test_a_device_is_filed_by_its_name_or_else_by_what_it_is():
    rec = {"usb": XIAO_STOCK, "class": hwsurvey.classify(XIAO_STOCK), "registered": []}
    assert hwsurvey.facts_key(rec) == "new-239a810b-2A2B2C2D2E2F3031"
    assert hwsurvey.facts_key({**rec, "registered": ["board7"]}) == "board7"
    heltec = {"usb": HELTEC, "class": hwsurvey.classify(HELTEC), "registered": []}
    assert hwsurvey.facts_key(heltec) == "new-303a1001-020000000010"


def test_a_device_with_no_chip_id_is_filed_under_its_host_and_port_too():
    disk = {"usb_vid": "0080", "usb_pid": "a001", "serial": "201503310007F", "busid": "2-2"}
    rec = {"usb": disk, "class": hwsurvey.classify(disk), "registered": []}
    assert hwsurvey.facts_key(rec, "bench-a") == "new-0080a001-bench-a-2-2-201503310007F"
    assert hwsurvey.facts_key(rec, "bench-b") != hwsurvey.facts_key(rec, "bench-a")


STOCK_E290 = {"usb_vid": "303a", "usb_pid": "0002", "serial": "020000000030",
              "product": "Heltec Vision Master E290", "busid": "1-1.4"}


def test_a_stock_app_on_native_usb_is_an_esp32_whose_serial_is_its_mac():
    c = hwsurvey.classify(STOCK_E290)
    assert c["kind"] == "esp32-usb-cdc" and c["mode"] == "app"
    assert c["ids"] == {"mac": "02:00:00:00:00:30"}
    record = {"usb": STOCK_E290, "class": c, "registered": [], "probes": {}}
    assert "usb:native-cdc" in hwsurvey.tags(record)
    assert hwsurvey.facts_key(record).endswith("020000000030")


EFUSE_JSON = {
    "MAC": {"value": "02:00:00:00:00:30 (OK)", "block": 1, "readable": True},
    "OPTIONAL_UNIQUE_ID": {"value": "4f 2b 72 77", "block": 2, "readable": True},
    "ADC1_CAL_VOL_ATTEN0": {"value": 86, "block": 2, "readable": True},
    "TEMP_CALIB": {"value": -15.2, "block": 2, "readable": True},
    "KEY_PURPOSE_0": {"value": "USER", "block": 0, "readable": True},
    "BLOCK_KEY0": {"value": "00 00 00 00 00 00", "block": 4, "readable": True},
    "BLOCK_KEY1": {"value": "de ad be ef 00 00", "block": 5, "readable": True},
    "BLOCK_KEY2": {"value": "?? ?? ?? ??", "block": 6, "readable": False},
    "BLOCK_USR_DATA": {"value": "00 00 00 01", "block": 3, "readable": True},
    "RD_DIS": {"value": "0", "block": 0, "readable": False},
}


def test_the_full_efuse_dump_keeps_the_calibration_and_never_a_secret_block():
    fields, blocks = hwsurvey.efuse_all(EFUSE_JSON)
    assert fields["ADC1_CAL_VOL_ATTEN0"] == 86 and fields["TEMP_CALIB"] == -15.2
    assert fields["OPTIONAL_UNIQUE_ID"] == "4f 2b 72 77" and fields["KEY_PURPOSE_0"] == "USER"
    assert fields["RD_DIS"] == "unreadable"
    assert not any(k.startswith("BLOCK_") for k in fields)
    assert "de ad be ef" not in repr((fields, blocks))
    assert blocks == {"BLOCK_KEY0": "empty", "BLOCK_KEY1": "burned",
                      "BLOCK_KEY2": "unreadable", "BLOCK_USR_DATA": "burned"}


def test_the_rom_mode_port_of_a_chip_is_named_by_its_mac():
    assert hwsurvey.rom_port_path("02:00:00:00:00:30") == (
        "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_02:00:00:00:00:30-if00")


@pytest.mark.plugins
def test_a_t114_running_a_stock_app_is_known_by_its_chip_id():
    c = hwsurvey.classify({"usb_vid": "239a", "usb_pid": "8029", "serial": "1A2B3C4D5E6F7081",
                           "product": "HT-n5262", "manufacturer": "Heltec"})
    assert (c["kind"], c["mcu"], c["mode"]) == ("heltec-t114", "nrf52840", "stock-app")
    assert c["ids"] == {"chip_id": "1A2B3C4D5E6F7081"}
    boot = hwsurvey.classify({"usb_vid": "239a", "usb_pid": "0071", "serial": "1A2B3C4D5E6F7081",
                              "product": "HT-n5262"})
    assert (boot["kind"], boot["mode"]) == ("heltec-t114", "bootloader")
    assert boot["ids"] == c["ids"], "one board in both modes"


@pytest.mark.plugins
def test_a_board_vendors_usb_ids_are_the_plugins_whose_board_it_is():
    """Core knows chip vendors' ids, bridges and probes. With no plugin's rows a XIAO is an
    unknown device. A plugin's row replaces one of core's (core's is a class's default; a
    firmware that does otherwise with the same ids says so), and the first plugin's row for an
    id stands."""
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families
    assert hwsurvey.classify(XIAO_BOOT)["kind"] == "xiao-nrf52840"
    hwsurvey.register_kinds({})
    try:
        assert hwsurvey.classify(XIAO_BOOT)["kind"] == "unknown"
        assert hwsurvey.classify(HELTEC)["kind"] == "esp32-usb-jtag", "a chip vendor's id is core's"
        hwsurvey.register_kinds({"303A:1001": {"kind": "mine"}, "1234:5678": {"kind": "widget"}})
        assert hwsurvey.classify(HELTEC)["kind"] == "mine"
        assert hwsurvey.classify({"usb_vid": "1234", "usb_pid": "5678"})["kind"] == "widget"
    finally:
        hwsurvey.register_kinds(families.usb_kinds())
    assert set(families.usb_kinds()) >= {"2886:0045", "239a:0071"}


def test_ids_read_off_the_usb_serial_say_so():
    """A chip id taken from the USB serial by a row's rule is assumed, not asked of the chip."""
    got = hwsurvey.classify({"usb_vid": "2fe3", "usb_pid": "0100", "serial": "0A0B0C0D0E0F1011"})
    assert got["ids"] == {"chip_id": "0A0B0C0D0E0F1011"} and got["ids_from"] == "usb serial"
    assert "ids_from" not in hwsurvey.classify({"usb_vid": "1a86", "usb_pid": "7523"})


def test_the_survey_matches_the_registry_the_way_devreg_does():
    """One rule for every reader: case does not matter, a sibling-qualified bridge counts only
    beside its sibling on the same hub, a block device matches by its by-id name."""
    hub = "/sys/devices/usb1/1-1/1-1.4"
    ch340 = {"usb_vid": "1a86", "usb_pid": "7523", "syspath": hub + "/1-1.4.1"}
    rp2040 = {"usb_vid": "2886", "usb_pid": "0050", "serial": "E66038B7135D4F2A", "syspath": hub + "/1-1.4.2"}
    stray = {"usb_vid": "1a86", "usb_pid": "7523", "syspath": "/sys/devices/usb1/1-1/1-1.2"}
    reg = {"board": {"kind": "serial", "match": {"usb_vid": "1A86", "usb_pid": "7523", "sibling_vid": "2886",
                                                 "sibling_pid": "0050", "sibling_serial": "e66038b7135d4f2a"}},
           "chip": {"kind": "serial", "match": {"usb_serial": "e66038b7135d4f2a"}},
           "disk": {"kind": "block", "match": {"block_by_id": "usb-Generic_Card-0:0"}}}
    assert hwsurvey.match_registry(ch340, reg, siblings=[ch340, rp2040, stray]) == ["board"]
    assert hwsurvey.match_registry(stray, reg, siblings=[ch340, rp2040, stray]) == [], "a stray bridge is nothing"
    assert hwsurvey.match_registry(ch340, reg) == [], "with no view of the hub, a sibling rule cannot pass"
    assert hwsurvey.match_registry(rp2040, reg) == ["chip"], "usb_serial compares without case"
    # the plug watcher's records carry a bus id, no sysfs path: the same hub rule holds
    a = {"usb_vid": "1a86", "usb_pid": "7523", "busid": "1-1.4.1"}
    b = {"usb_vid": "2886", "usb_pid": "0050", "serial": "E66038B7135D4F2A", "busid": "1-1.4.2"}
    assert hwsurvey.match_registry(a, reg, siblings=[a, b]) == ["board"]


def test_a_sibling_named_bridge_is_its_board_not_a_bare_vid_pid_entrys():
    """As devreg: an entry that names the device by a distinguishing key outranks one that
    matches it by make alone (board3's CH340 is not cyd1)."""
    ch340 = {"usb_vid": "1a86", "usb_pid": "7523", "busid": "1-1.4.1"}
    rp2040 = {"usb_vid": "2886", "usb_pid": "0050", "serial": "E66038B7135D4F2A", "busid": "1-1.4.2"}
    other = {"usb_vid": "1a86", "usb_pid": "7523", "busid": "1-1.3"}
    reg = {"cyd1": {"kind": "serial", "match": {"usb_vid": "1a86", "usb_pid": "7523"}},
           "board3": {"kind": "serial", "match": {"usb_vid": "1a86", "usb_pid": "7523", "sibling_vid": "2886",
                                                "sibling_serial": "e66038b7135d4f2a"}}}
    seen = [ch340, rp2040, other]
    assert hwsurvey.match_registry(ch340, reg, siblings=seen) == ["board3"]
    assert hwsurvey.match_registry(other, reg, siblings=seen) == ["cyd1"]


def test_a_name_two_devices_took_is_neithers():
    recs = [{"usb": {"busid": "1-1.3"}, "registered": ["cyd1"]},
            {"usb": {"busid": "1-1.4.1"}, "registered": ["cyd1"]},
            {"usb": {"busid": "1-1.2"}, "registered": ["kb1"]}]
    assert hwsurvey.ambiguous_names(recs) == {"cyd1": ["1-1.3", "1-1.4.1"]}


@pytest.mark.parametrize("ifs, is_input", [
    (["03/01/02"], True),                    # a mouse
    (["03/01/01", "03/00/00"], True),        # a keyboard with a second HID interface
    (["03/00/00"], False),                   # HID with no boot protocol: a Teensy's bootloader
    (["02/02/00", "0a/00/00"], False),       # a CDC serial port
    (["03/01/01", "02/02/00", "0a/00/00"], False),   # a keyboard firmware with a console port
    ([], False)])
def test_a_keyboard_or_mouse_is_input_and_a_hid_bootloader_is_not(ifs, is_input):
    usb = {"usb_vid": "04d8", "usb_pid": "eb2d", "interfaces": ifs}
    assert hwsurvey.is_input(usb) is is_input
    assert (hwsurvey.classify(usb)["kind"] == "input") is is_input
