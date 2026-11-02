"""The state-line protocol as PLUGINS.md defines it, with vectors built here, not taken from one
firmware's output (docs/PLUGINS.md#the-state-line)."""
import binascii
import json

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import stateline


def line(obj, crc=None):
    text = json.dumps(obj, separators=(",", ":"))
    return f"~S{text}*{binascii.crc_hqx(text.encode(), 0) if crc is None else crc:04x}"


def test_a_line_is_its_json_checked_by_crc16_xmodem():
    obj = {"v": 1, "family": "widget", "fw": "1.2.0+abc123"}
    assert stateline.parse_line("prompt> state\n" + line(obj) + "\nprompt> ") == obj
    # CRC-16/XMODEM's check value: "123456789" -> 0x31c3
    assert binascii.crc_hqx(b"123456789", 0) == 0x31C3


def test_no_line_is_none_and_a_damaged_one_is_refused():
    assert stateline.parse_line("no state here") is None
    with pytest.raises(stateline.StateError, match="checksum"):
        stateline.parse_line(line({"v": 1}, crc=0))
    with pytest.raises(stateline.StateError, match="incomplete"):
        stateline.parse_line('~S{"v":1')


def test_placeholders_are_null_after_the_checksum():
    text = '{"v":1,"rssi":nan}'
    raw = f"~S{text}*{binascii.crc_hqx(text.encode(), 0):04x}"
    assert stateline.parse_line(raw, unknown=("nan",)) == {"v": 1, "rssi": None}


def test_the_example_in_plugins_md_parses():
    import os
    import re
    doc = open(os.path.join(os.path.dirname(__file__), "..", "..", "docs", "PLUGINS.md")).read()
    example = re.search(r"^    (~S\{.*\}\*[0-9a-f]{4})$", doc, re.M).group(1)
    assert stateline.parse_line(example)["family"] == "widget"
