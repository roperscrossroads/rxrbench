"""The plug watcher's one question to a device (host/plugwatch.py,
docs/ARCHITECTURE.md#plug-watcher-and-busy-marks): a bootloader a board type declares as hiding the chip id is
asked which registered id selects it; read-only, once per enumeration, never under a busy mark."""
import importlib.util
import json
import os
import subprocess
import sys
import time
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "plugwatch.py")


@pytest.fixture
def pw(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyudev", types.ModuleType("pyudev"))
    spec = importlib.util.spec_from_file_location("plugwatch", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


POG, KB = "0A0B0C0D0E0F1011", "1011121314151617"
CATALOG = {"boards": {
    "sparkfun-pro-micro-rp2040": {"uf2": {"bootloader_usb": ["2e8a:0003"], "hides_chip_id": True,
                                          "identify": "picotool"}},
    "adafruit-kb2040": {"uf2": {"bootloader_usb": ["2e8a:0003"], "hides_chip_id": True,
                                "identify": "picotool"}},
    "raspberrypi-pico2w": {"uf2": {"bootloader_usb": ["2e8a:000f"]}}}}
REGISTRY = {"devices": {
    "pog1": {"policy": {"board_tag": "sparkfun-pro-micro-rp2040"}, "match": {"usb_serial": POG}},
    "kb1": {"policy": {"board_tag": "adafruit-kb2040"}, "match": {"usb_serial": KB}},
    "pico2w": {"policy": {"board_tag": "raspberrypi-pico2w"}, "match": {"usb_serial": "0123456789ABCDEF"}},
    "xiao": {"policy": {"board_tag": "xiao"}, "match": {"usb_serial": "1A1B1C1D1E1F2021"}}}}


def rom(devnum="92", busid="1-1.2.1"):
    return {"busid": busid, "usb_vid": "2e8a", "usb_pid": "0003", "serial": "A1A2A3A4A5A6",
            "busnum": "1", "devnum": devnum}


class Ask:
    """A fake identity query: the chip id each (bus, address) answers to; every call logged."""

    def __init__(self, at):
        self.at, self.calls = at, []

    def __call__(self, d, chip_id):
        self.calls.append((d["devnum"], chip_id))
        return self.at.get(d["devnum"]) == chip_id


def test_only_declared_bootloaders_are_asked_and_their_candidates_are_the_types_boards(pw):
    hiding = pw.hiding_bootloaders(CATALOG, REGISTRY)
    assert hiding == {"2e8a:0003": {"identify": "picotool", "ids": sorted([POG, KB])}}


def test_the_id_that_selects_the_bootloader_is_its_chip_id(pw):
    ask = Ask({"92": POG})
    devs = [rom(), {"busid": "1-1.3", "usb_vid": "303a", "usb_pid": "1001", "serial": "AC:A7:04"}]
    pw.identify(devs, pw.hiding_bootloaders(CATALOG, REGISTRY), (set(), set()), asked={},
                queries={"picotool": ask})
    assert devs[0]["chip_id"] == POG and devs[0]["serial_is_id"] is False
    assert devs[0]["identified_by"] == "bootloader"
    assert "chip_id" not in devs[1] and "serial_is_id" not in devs[1], "an undeclared device is not asked"
    assert {c for _n, c in ask.calls} <= {POG, KB}


def test_a_bootloader_no_registered_id_selects_stays_unidentified(pw):
    devs = [rom()]
    pw.identify(devs, pw.hiding_bootloaders(CATALOG, REGISTRY), (set(), set()), asked={},
                queries={"picotool": Ask({})})
    assert "chip_id" not in devs[0] and devs[0]["identify_error"] == "no registered chip id selects it"


def test_one_enumeration_is_asked_once_and_a_new_one_again(pw):
    ask, asked = Ask({"92": POG, "93": POG}), {}
    hiding = pw.hiding_bootloaders(CATALOG, REGISTRY)
    for _ in range(3):
        devs = [rom()]
        pw.identify(devs, hiding, (set(), set()), asked=asked, queries={"picotool": ask})
        assert devs[0]["chip_id"] == POG
    n = len(ask.calls)
    devs = [rom(devnum="93")]                       # replugged: a new enumeration
    pw.identify(devs, hiding, (set(), set()), asked=asked, queries={"picotool": ask})
    assert len(ask.calls) > n and list(asked) == [("1-1.2.1", "1", "93")], "the old one forgotten"


@pytest.mark.parametrize("busy", [({"1-1.2.1"}, set()), (set(), {POG})])
def test_a_busy_mark_holds_the_question_back(pw, busy):
    ask = Ask({"92": POG})
    devs = [rom()]
    pw.identify(devs, pw.hiding_bootloaders(CATALOG, REGISTRY), busy, asked={},
                queries={"picotool": ask})
    assert ask.calls == [] and devs[0]["identify_deferred"] is True and "chip_id" not in devs[0]


def test_a_failing_query_is_said_and_the_watcher_goes_on(pw):
    def boom(d, c):
        raise subprocess.TimeoutExpired("picotool", 10)
    devs = [rom()]
    pw.identify(devs, pw.hiding_bootloaders(CATALOG, REGISTRY), (set(), set()), asked={},
                queries={"picotool": boom})
    assert "picotool" in devs[0]["identify_error"] and "chip_id" not in devs[0]
    assert devs[0]["identify_deferred"] is True, "an error is asked again at the next look"


def test_two_ids_that_both_select_it_are_not_a_guess(pw):
    devs = [rom()]
    pw.identify(devs, pw.hiding_bootloaders(CATALOG, REGISTRY), (set(), set()), asked={},
                queries={"picotool": lambda d, c: True})
    assert "chip_id" not in devs[0] and "more than one" in devs[0]["identify_error"]


def test_busy_marks_give_ports_and_chip_ids_of_live_marks_only(pw, tmp_path):
    now = time.time()
    (tmp_path / "pog1.json").write_text(json.dumps({"board": "pog1", "busids": [], "keys": [POG.lower()],
                                                    "until": now + 60}))
    (tmp_path / "old.json").write_text(json.dumps({"board": "x", "busids": ["1-9"], "keys": ["X"],
                                                   "until": now - 1}))
    assert pw.busy_marks(str(tmp_path), now=now) == (set(), {POG})


def test_picotool_is_asked_for_that_bus_and_address_only_and_its_not_found_is_no(pw):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        # picotool 2.1's answer for another id at that bus and address, exit status 249
        out = "No accessible RP-series device in BOOTSEL mode was found at bus 1, address 92."
        return subprocess.CompletedProcess(argv, 0 if argv[-1] == POG else 249,
                                           "Program Information\n none\n" if argv[-1] == POG else out, "")
    assert pw.ask_picotool(rom(), POG, run=run) is True
    assert pw.ask_picotool(rom(), KB, run=run) is False, "picotool's 'not this one' is a no"
    assert seen[0] == ["picotool", "info", "--bus", "1", "--address", "92", "--ser", POG]


def test_picotool_failing_for_another_reason_is_an_error_not_a_no(pw):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "ERROR: Unable to access device")
    with pytest.raises(OSError, match="Unable to access"):
        pw.ask_picotool(rom(), POG, run=run)
    devs = [rom()]
    pw.identify(devs, pw.hiding_bootloaders(CATALOG, REGISTRY), (set(), set()), asked={},
                queries={"picotool": lambda d, c: pw.ask_picotool(d, c, run=run)})
    assert "Unable to access" in devs[0]["identify_error"]


def test_an_error_is_not_kept_for_the_enumeration_but_an_answer_is(pw):
    hiding, asked, calls = pw.hiding_bootloaders(CATALOG, REGISTRY), {}, []

    def flaky(d, c):
        calls.append(c)
        if len(calls) == 1:
            raise OSError("picotool info failed (rc 1): ERROR: Unable to access device")
        return c == POG
    devs = [rom()]
    pw.identify(devs, hiding, (set(), set()), asked=asked, queries={"picotool": flaky})
    assert "identify_error" in devs[0] and asked == {}
    devs = [rom()]
    pw.identify(devs, hiding, (set(), set()), asked=asked, queries={"picotool": flaky})
    assert devs[0]["chip_id"] == POG and "identify_error" not in devs[0]
    n = len(calls)
    pw.identify([rom()], hiding, (set(), set()), asked=asked, queries={"picotool": flaky})
    assert len(calls) == n, "a definite answer is not asked again"
