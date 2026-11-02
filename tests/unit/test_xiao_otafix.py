"""xiao_otafix.py: which bootloader an update is and which board it is for, the gates that
decide whether an installed image needs the carrier first, and the board lock it shares with
core's uf2_flash.py."""
import importlib.util
import os

import pytest

BIN = os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections", "rxrbench")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, *path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_bootloader_update_and_a_uf2_run_keep_each_other_off_the_board(monkeypatch, tmp_path):
    monkeypatch.setenv("UF2_FLASH_REEXEC", "1")       # importing must not re-exec under another python
    monkeypatch.setenv("DEVLOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("UF2_LOCK_HELD", raising=False)
    otafix = _load("xiao_otafix", ("mesh", "host", "bin", "xiao_otafix.py"))
    uf2 = _load("uf2_flash", ("core", "host", "bin", "uf2_flash.py"))
    held = otafix.lock_board("0123456789ABCDEF")
    assert held is not None
    with pytest.raises(SystemExit, match="already working on"):
        uf2.board_lock("0123456789ABCDEF")
    monkeypatch.setenv("UF2_LOCK_HELD", otafix.LOCKED.lower())   # what the update tool passes on
    assert uf2.board_lock("0123456789ABCDEF") is None


def _uf2(tmp_path, text):
    """A UF2 file whose one data block carries `text` (a bootloader's own words are in its binary)."""
    import struct
    data = text.encode().ljust(256, b"\0")
    block = struct.pack("<8I", 0x0A324655, 0x9E5D5157, 0x2000, 0xF4000, 256, 0, 1, 0xD663823C)
    block += data + b"\0" * (476 - 256) + struct.pack("<I", 0x0AB16F30)
    path = tmp_path / "update.uf2"
    path.write_bytes(block)
    return str(path)


def test_an_update_says_which_bootloader_it_is_and_which_board_it_is_for(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "xiao_otafix", os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections", "rxrbench", "mesh", "host", "bin",
                                    "xiao_otafix.py"))
    otafix = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(otafix)
    payload = otafix.payload_of(_uf2(
        tmp_path, "UF2 Bootloader 0.9.2-OTAFIX2.5-zt1 lib\r\nModel: Seeed XIAO nRF52840\r\n"
                  "Board-ID: nRF52840-SeeedXiaoSense-v1\r\n"))
    assert otafix.embedded_version(payload) == "0.9.2-OTAFIX2.5-zt1"
    assert b"Model: Seeed XIAO nRF52840" in payload
    assert b"Board-ID: nRF52840-SeeedXiao-v1" not in payload       # another board's update is refused
    assert otafix.embedded_version(b"nothing here") is None


# ---- xiao_otafix.decide(): what may be written, and whether a carrier goes on first ----------
def _otafix():
    spec = importlib.util.spec_from_file_location(
        "xiao_otafix", os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections", "rxrbench", "mesh", "host", "bin",
                                    "xiao_otafix.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BOARD = dict(model="Seeed XIAO nRF52840", board="nRF52840-SeeedXiaoSense-v1",
             sd="S140 version 7.3.0", stock=False)
UPDATE_TEXT = (b"UF2 Bootloader 0.9.2-OTAFIX2.5-zt1\r\nModel: Seeed XIAO nRF52840\r\n"
               b"Board-ID: nRF52840-SeeedXiaoSense-v1\r\n")


def test_an_image_below_the_staging_area_is_written_without_a_carrier():
    o = _otafix()
    go, carrier, _ = o.decide(custom=True, ver="0.9.2-OTAFIX2.5", want="0.9.2-OTAFIX2.5-zt1",
                              payload=UPDATE_TEXT, end=0xBC924, carrier="/c12.uf2", **BOARD)
    assert (go, carrier) == (True, False)


def test_an_image_that_reaches_the_staging_area_takes_the_carrier_first():
    o = _otafix()
    go, carrier, lines = o.decide(custom=True, ver="0.9.2-OTAFIX2.5", want="0.9.2-OTAFIX2.5-zt1",
                                  payload=UPDATE_TEXT, end=0xE18A0, carrier="/c12.uf2", **BOARD)
    assert (go, carrier) == (True, True) and "carrier image goes on first" in lines[-1]


def test_without_a_carrier_an_overlapping_image_is_a_stop_that_names_the_way_out():
    o = _otafix()
    go, carrier, lines = o.decide(custom=True, ver="0.9.2-OTAFIX2.5", want="0.9.2-OTAFIX2.5-zt1",
                                  payload=UPDATE_TEXT, end=0xE18A0, carrier=None, **BOARD)
    assert (go, carrier) == (False, False)
    assert lines[-1].startswith("STOP") and "--carrier" in lines[-1] and "Nothing written" in lines[-1]


def test_a_carrier_is_never_given_to_a_stock_app_or_an_unknown_one():
    o = _otafix()
    stock = dict(BOARD, stock=True)
    for kw, end in ((stock, 0xE18A0), (BOARD, None)):
        go, carrier, lines = o.decide(custom=True, ver="0.9.2-OTAFIX2.5",
                                      want="0.9.2-OTAFIX2.5-zt1", payload=UPDATE_TEXT, end=end,
                                      carrier="/c12.uf2", **kw)
        assert (go, carrier) == (False, False) and lines[-1].startswith("STOP")


def test_the_other_gates_still_stop_before_a_carrier_is_considered():
    o = _otafix()
    # a custom update onto a factory bootloader, onto itself, and for another board
    for ver, want, payload in (("0.6.1", "0.9.2-OTAFIX2.5-zt1", UPDATE_TEXT),
                               ("0.9.2-OTAFIX2.5-zt1", "0.9.2-OTAFIX2.5-zt1", UPDATE_TEXT),
                               ("0.9.2-OTAFIX2.5", "x", b"Board-ID: another\r\n")):
        go, carrier, _ = o.decide(custom=True, ver=ver, want=want, payload=payload, end=0xE18A0,
                                  carrier="/c12.uf2", **BOARD)
        assert (go, carrier) == (False, False)
    # the pinned release path: only from 0.6.1 / S140 7.3.0, and already-OTAFIX is a no-op
    assert o.decide(custom=False, ver="0.9.2-OTAFIX2.5", want=None, payload=b"", end=0xBC000,
                    carrier=None, **BOARD)[0] is False
    assert o.decide(custom=False, ver="0.6.1", want=None, payload=b"", end=0xBC000, carrier=None,
                    **BOARD)[0] is True
    assert o.decide(custom=False, ver="0.6.1", want=None, payload=b"", end=0xBC000, carrier=None,
                    **dict(BOARD, sd="S132 version 6.1.1"))[0] is False
