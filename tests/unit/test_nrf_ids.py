"""nrf_ids.py's decoding, against a synthetic nRF52840 memory image (a made-up chip)."""
import importlib.util
import os

SPEC = importlib.util.spec_from_file_location(
    "nrf_ids", os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections", "rxrbench", "core", "host", "bin", "nrf_ids.py"))
nrf_ids = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nrf_ids)


def image(base_words, sets):
    words = [0xFFFFFFFF] * base_words
    for offset, value in sets.items():
        words[offset // 4] = value
    return words


FICR = image(nrf_ids.FICR_WORDS, {
    0x10: 4096, 0x14: 256, 0x60: 0x01234567, 0x64: 0x89ABCDEF, 0x80: 0xDEADBEEF,
    0x90: 0xCAFEF00D, 0xA0: 1, 0xA4: 0x44332211, 0xA8: 0xFFFF6655,
    0x100: 0x52840, 0x104: 0x41414430, 0x108: 0x2004, 0x10C: 256, 0x110: 1024})
UICR = image(nrf_ids.UICR_WORDS, {
    0x14: 0xF4000, 0x18: 0xFF000, 0x208: 0xFFFFFFFF, 0x20C: 0xFFFFFFFF, 0x304: 5,
    0x80: 0x12345678})


def test_the_ficr_gives_the_ids_and_sizes_and_withholds_the_roots():
    f = nrf_ids.decode_ficr(FICR)
    assert f["device_id"] == "89abcdef01234567"
    assert f["device_addr"] == "66:55:44:33:22:11" and f["device_addr_type"] == "random"
    assert f["part"] == "nRF52840" and f["variant"] == "AAD0"
    assert (f["ram_kb"], f["flash_kb"], f["code_page_size"], f["code_pages"]) == (256, 1024, 4096, 256)
    assert "deadbeef" not in repr(f).lower() and "cafef00d" not in repr(f).lower()


def test_the_uicr_says_where_the_bootloader_is_and_what_the_regulator_outputs():
    u = nrf_ids.decode_uicr(UICR)
    assert u["bootloader_addr"] == 0xF4000 and u["bootloader_settings_addr"] == 0xFF000
    assert u["regout0"] == "3.3 V" and u["nfc_pins"] == "nfc" and u["approtect"] == "disabled"
    assert u["customer"] == {0: "0x12345678"}
    assert u["pselreset"] == [None, None]


def test_an_erased_uicr_reads_as_not_set():
    u = nrf_ids.decode_uicr([0xFFFFFFFF] * nrf_ids.UICR_WORDS)
    assert u["bootloader_addr"] is None and u["regout0"] == "not set" and u["customer"] == {}
    assert u["nfc_pins"] == "nfc"          # erased = the chip's default: the NFC antenna pins


def test_a_board_that_uses_the_nfc_pins_as_gpio_says_so():
    words = list(UICR)
    words[0x20C // 4] = 0xFFFFFFFE
    assert nrf_ids.decode_uicr(words)["nfc_pins"] == "gpio"
