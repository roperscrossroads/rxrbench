"""The relay posts a USB change only once it has held across two readings."""
import importlib.util
import os

import pytest

spec = importlib.util.spec_from_file_location(
    "plug_relay", os.path.join(os.path.dirname(__file__), "..", "..", "tools", "plug_relay.py"))
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

A = {"busid": "1-1.1", "usb_vid": "2fe3", "usb_pid": "0004", "serial": "3A3B3C3D3E3F4041"}
B = {"busid": "1-1.2", "usb_vid": "303a", "usb_pid": "1001", "serial": "02:00:00:00:00:10"}
NEW = {"busid": "1-1.4", "usb_vid": "239a", "usb_pid": "810b", "serial": "AAAABBBBCCCCDDDD"}


def test_the_first_reading_is_a_baseline_and_posts_nothing():
    action, added, removed, st = relay.decide({}, [A, B])
    assert action == "baseline" and not added and not removed
    assert relay.decide(st, [A, B])[0] == "none"


def test_a_new_board_is_reported_on_the_second_reading_that_still_shows_it():
    st = relay.decide({}, [A, B])[3]
    action, _, _, st = relay.decide(st, [A, B, NEW])
    assert action == "wait"
    action, added, removed, st = relay.decide(st, [A, B, NEW])
    assert action == "report" and added == [NEW] and removed == []
    assert relay.decide(st, [A, B, NEW])[0] == "none"


def test_a_board_re_enumerating_during_a_flash_is_not_reported():
    st = relay.decide({}, [A, B])[3]
    st = relay.decide(st, [B])[3]                 # gone for one reading (UF2 mode, say)
    action, _, _, st = relay.decide(st, [A, B])   # back
    assert action == "none" and st["pending"] is None


def test_a_removal_is_reported_by_identity():
    st = relay.decide({}, [A, B])[3]
    st = relay.decide(st, [B])[3]
    action, added, removed, _ = relay.decide(st, [B])
    assert action == "report" and removed == ["3A3B3C3D3E3F4041"] and not added


def test_a_device_without_a_serial_is_keyed_by_its_port():
    d = {"busid": "2-2", "usb_vid": "1a86", "usb_pid": "7523", "serial": None}
    assert relay.key(d) == "1a86:7523:port-2-2"


def test_a_board_is_named_and_described_from_its_facts(monkeypatch, widgets):
    monkeypatch.setattr(relay, "registry_descriptions",
                        lambda: {"board7": "Seeed XIAO nRF52840 + Wio-SX1262 kit — more"})
    rec = {"registered": ["board7"], "tags": ["mcu:nrf52840"],
           "class": {"kind": "zephyr-cdc", "ids": {"chip_id": "3A3B3C3D3E3F4041"}},
           "probes": {"widget": {"class": 1, "board": "xiao_ble"}}}
    name, lines = relay.identify(A, rec)
    assert name == "board7"
    assert lines[0] == "Seeed XIAO nRF52840 + Wio-SX1262 kit"
    assert "widget class 1 on xiao_ble" in lines and lines[-1] == "USB port 1-1.1"


def test_a_board_with_no_facts_is_new_and_still_says_what_it_is(widgets):
    name, lines = relay.identify({**NEW, "usb_vid": "f1d1", "usb_pid": "0002"}, None)
    assert name == "NEW wm1"
    assert "chip_id AAAABBBBCCCCDDDD" in lines


def test_a_xiao_changing_mode_is_the_same_board_not_an_unplug_and_a_plug():
    stock = {**A, "usb_vid": "239a", "usb_pid": "810b"}
    st = relay.decide({}, [stock, B])[3]
    assert relay.decide(st, [A, B])[0] == "none"


def test_state_from_another_key_format_starts_a_new_baseline(tmp_path, monkeypatch):
    import json
    (tmp_path / "plug-h.json").write_text(json.dumps({"reported": ["2fe3:0004:X"], "pending": None}))
    monkeypatch.setattr(relay, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(relay, "snapshot", lambda h: {"devices": [A, B]})
    monkeypatch.setattr(relay.sys, "argv", ["x", "--hosts", "h"])
    assert relay.main() == 0
    st = json.loads((tmp_path / "plug-h.json").read_text())
    assert st["v"] == relay.STATE_VERSION and st["pending"] is None


def test_a_shared_serial_is_told_apart_by_host():
    a = {"host": "host-a", "registered": ["board-a"], "usb": {"serial": "0001"}}
    b = {"host": "host-b", "registered": ["board-b"], "usb": {"serial": "0001"}}
    known = {"0001": [a, b]}
    assert relay.lookup(known, {"serial": "0001"}, "host-b") is b
    assert relay.lookup(known, {"serial": "0001"}, "host-c") is None


def test_a_unique_serial_needs_no_host():
    a = {"host": "host-a", "usb": {"serial": "ABCDEF0123456789"}}
    assert relay.lookup({"ABCDEF0123456789": [a]}, {"serial": "ABCDEF0123456789"}, "host-z") is a
    assert relay.lookup({}, {"serial": "nope"}, "host-a") is None


def test_a_serialless_bridge_is_named_by_its_sibling(tmp_path, monkeypatch):
    reg = tmp_path / "registry.yaml"
    reg.write_text("devices:\n  boardx:\n    match: {usb_vid: '1a86', usb_pid: '7523', "
                   "sibling_vid: '2886', sibling_serial: 'E66038B7135D4F2A'}\n")
    monkeypatch.setattr(relay, "REGISTRY", str(reg))
    bridge = {"usb_vid": "1a86", "usb_pid": "7523", "busid": "1-1.4.1.1"}
    ui = {"usb_vid": "2886", "usb_pid": "0050", "serial": "E66038B7135D4F2A", "busid": "1-1.4.1.2"}
    stray = {"usb_vid": "1a86", "usb_pid": "7523", "busid": "1-1.2"}
    assert relay.sibling_name(bridge, [bridge, ui, stray]) == "boardx"
    assert relay.sibling_name(stray, [bridge, ui, stray]) is None, "a stray bridge elsewhere"
    assert relay.sibling_name(bridge, [bridge, stray]) is None, "no sibling, no name"


def test_an_esp32_stock_app_and_its_rom_face_are_one_board():
    """A stock app on native USB carries the MAC without colons; the ROM and USB-Serial-JTAG
    faces carry it with them. The first flash must not read as an unplug and a plug."""
    stock = {**B, "usb_vid": "303a", "usb_pid": "0002", "serial": "020000000010"}
    assert relay.key(stock) == relay.key(B) == "02:00:00:00:00:10"
    st = relay.decide({}, [A, stock])[3]
    assert relay.decide(st, [A, B])[0] == "none"


def test_a_bench_host_that_is_this_machine_is_read_in_place(tmp_path, monkeypatch):
    hosts = tmp_path / "layouts" / "hosts"
    hosts.mkdir(parents=True)
    (hosts / "hosts.yml").write_text(
        "all:\n  children:\n    bench_hosts:\n      vars: {ansible_connection: local}\n"
        "      hosts:\n        pi1: {}\n        far1: {ansible_connection: ssh}\n")
    state = tmp_path / "home" / "rxrbench" / "state"
    state.mkdir(parents=True)
    (state / "usb-present.json").write_text('{"devices": [{"usb_vid": "303a"}]}')
    (state / "busy").mkdir()
    (state / "busy" / "pog1.json").write_text('{"board": "pog1", "busids": ["1-1.3"], "until": 9e9}')
    monkeypatch.setenv("RXRBENCH_SITE", str(tmp_path))
    monkeypatch.setenv("RXRBENCH_HOST_STATE", str(state))
    assert relay.site.local_hosts() == {"pi1"}
    monkeypatch.setattr(relay.subprocess, "run", lambda *a, **k: pytest.fail("no ssh"))
    assert relay.snapshot("pi1") == {"devices": [{"usb_vid": "303a"}],
                                     "busy": [{"board": "pog1", "busids": ["1-1.3"], "until": 9e9}]}



# A board being flashed leaves the bus and comes back, first as its bootloader (another USB
# device on the same port, here an RP2040's ROM with a serial of its own).
POG = {"busid": "1-1.3", "usb_vid": "1b4f", "usb_pid": "0026", "serial": "1B2B3B4B5B6B7B8B"}
BOOT = {"busid": "1-1.3", "usb_vid": "2e8a", "usb_pid": "0003", "serial": "0A1B2C3D4E5F"}
MARK = {"board": "pog1", "busids": ["1-1.3"], "keys": [], "until": 2e9}


def test_a_busy_board_and_its_port_are_held_as_last_seen(tmp_path):
    names = {"pog1": {"1B2B3B4B5B6B7B8B"}}
    st = relay.decide({}, [A, POG])[3]
    for during in ([A], [A, BOOT], [A, POG]):          # gone, its bootloader, back
        now = relay.hold_busy(during, [MARK], [A, POG], names=names, now=1e9)
        action, added, removed, st = relay.decide(st, now)
        assert action == "none" and not added and not removed
    # moved to another port while busy: still the board (by its chip id), not news
    moved = {**POG, "busid": "1-1.4"}
    assert relay.decide(st, relay.hold_busy([A, moved], [MARK], [A, POG], names=names,
                                            now=1e9))[0] == "none"


def test_a_mark_past_its_time_holds_nothing():
    old = {**MARK, "until": 5}
    assert relay.hold_busy([A, BOOT], [old], [A, POG], names={}, now=1e9) == [A, BOOT]


def test_once_the_mark_is_gone_a_board_that_came_back_different_is_news():
    st = relay.decide({}, [A, POG])[3]
    st = relay.decide(st, [A, BOOT])[3]
    action, added, removed, _ = relay.decide(st, [A, BOOT])
    assert action == "report" and added == [BOOT] and removed == ["1B2B3B4B5B6B7B8B"]


def test_a_remote_snapshot_carries_the_busy_marks(monkeypatch):
    out = ('{"devices": [{"usb_vid": "303a"}]}\n' + relay.BUSY_SEP + "\n"
           + '{"board": "pog1", "busids": ["1-1.3"]}\n')
    monkeypatch.setattr(relay.site, "local_hosts", lambda: set())
    monkeypatch.setattr(relay.site, "bench_hosts", lambda: {"far1": ("bench", "far1")})
    monkeypatch.setattr(relay.subprocess, "run",
                        lambda *a, **k: relay.subprocess.CompletedProcess(a, 0, out, ""))
    assert relay.snapshot("far1") == {"devices": [{"usb_vid": "303a"}],
                                      "busy": [{"board": "pog1", "busids": ["1-1.3"]}]}


def test_marks_whose_time_is_up_are_deleted_and_the_rest_kept(tmp_path):
    (tmp_path / "old.json").write_text('{"board": "old", "until": 100}')
    (tmp_path / "live.json").write_text('{"board": "live", "until": 300}')
    (tmp_path / "torn.json").write_text('{"board": ')
    assert relay.read_marks(str(tmp_path), now=200) == [{"board": "live", "until": 300}]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live.json", "torn.json"]


def test_the_remote_reader_does_the_same_on_the_host(tmp_path):
    state = tmp_path / "rxrbench" / "state"
    (state / "busy").mkdir(parents=True)
    (state / "usb-present.json").write_text('{"devices": []}')
    (state / "busy" / "old.json").write_text('{"board": "old", "until": 1}')
    (state / "busy" / "live.json").write_text('{"board": "live", "until": 9e9}')
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-c", relay.REMOTE], capture_output=True, text=True,
                         env={**os.environ, "HOME": str(tmp_path)}, check=True).stdout
    head, _, tail = out.partition(relay.BUSY_SEP)
    assert head.strip() == '{"devices": []}' and tail.split() == ['{"board":', '"live",', '"until":', '9000000000.0}']
    assert not (state / "busy" / "old.json").exists()


def test_a_board_no_family_found_anything_on_is_described_by_none(monkeypatch, widgets):
    # every loaded family is asked; one whose probe found nothing adds no line (no "widget ?")
    name, lines = relay.identify(POG, {"registered": ["pog1"], "probes": {}, "class": {"kind": "unknown"}})
    assert name == "pog1" and not any(ln.startswith("widget") for ln in lines)
    _, found = relay.identify(POG, {"registered": ["pog1"], "probes": {"widget": {}},
                                    "class": {"kind": "unknown"}})
    assert "widget class ?" in found      # found by its probe, nothing more known


def test_a_bootloader_the_watcher_identified_is_described_as_its_board():
    pog = {"host": "pi1", "usb": {"serial": "0A0B0C0D0E0F1011"}}
    rom = {"serial": "A1A2A3A4A5A6", "chip_id": "0A0B0C0D0E0F1011", "serial_is_id": False,
           "busid": "1-1.2.1", "usb_vid": "2e8a", "usb_pid": "0003"}
    assert relay.lookup({"0A0B0C0D0E0F1011": [pog]}, rom, "pi1") is pog
    assert relay.key(rom) == "0A0B0C0D0E0F1011", "the same key as in its app: no plug change"
