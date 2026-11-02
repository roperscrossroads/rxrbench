import pytest

import importlib.util
import os

pytest.importorskip("usb", reason="needs pyusb")
_spec = importlib.util.spec_from_file_location(
    "cp210x_serial", os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections", "rxrbench", "core", "host", "bin", "cp210x_serial.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def test_the_serial_goes_in_as_a_usb_string_descriptor():
    assert cp.descriptor("board-a") == bytes([16, 3]) + "board-a".encode("utf-16-le")


def test_changed_ranges_names_exactly_the_bytes_that_differ():
    a = bytes(16)
    b = bytearray(a)
    b[3:6] = b"\x01\x02\x03"
    b[15] = 9
    assert cp.changed_ranges(a, bytes(b)) == [(3, 6), (15, 16)]


@pytest.mark.parametrize("value,ok", [("board-a", True), ("board_b.2", True), ("BOARD-A", True),
                                      ("", False), ("a b", False), ("x" * 33, False),
                                      ("é", False)])
def test_only_plain_short_serials_are_accepted(value, ok):
    assert bool(cp.SERIAL_RE.match(value)) is ok


def test_nothing_in_the_tool_can_write_the_lock_register():
    import inspect
    src = inspect.getsource(cp)
    writes = [line for line in src.splitlines() if "VENDOR_OUT" in line and "ctrl_transfer" in line]
    assert writes and all("REG_SERIAL_NUMBER" in line for line in writes)
