import json
import os
import time

from ansible_collections.rxrbench.core.plugins.module_utils import location

XIAO = {"match": {"usb_serial": "1A1B1C1D1E1F2021"}, "reach": {"usb": {"host": "bench-a"}}}
HELTEC = {"match": {"serial_by_id": "usb-Espressif_USB_JTAG_serial_debug_unit_02:00:00:00:00:10-if00"}}
BRIDGE = {"match": {"host": "bench-b", "serial_by_id":
                    "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0"}}


def state(tmp_path, host, keys, age=0):
    p = tmp_path / f"plug-{host}.json"
    p.write_text(json.dumps({"v": 2, "reported": keys}))
    t = time.time() - age
    os.utime(p, (t, t))


def test_a_board_is_found_by_its_chip_id_or_mac_wherever_it_is(tmp_path):
    state(tmp_path, "bench-b", ["1A1B1C1D1E1F2021"])
    state(tmp_path, "bench-a", ["02:00:00:00:00:10"])
    idx = location.seen(str(tmp_path))
    assert location.locate(XIAO, "bench-a", idx) == ("bench-b", "seen")
    assert location.locate(HELTEC, None, idx) == ("bench-a", "seen")


def test_a_board_not_seen_falls_back_to_the_hint(tmp_path):
    assert location.locate(XIAO, "bench-a", location.seen(str(tmp_path))) == ("bench-a", "hint")


def test_a_stale_index_is_not_trusted(tmp_path):
    state(tmp_path, "bench-b", ["1A1B1C1D1E1F2021"], age=3600)
    assert location.locate(XIAO, "bench-a", location.seen(str(tmp_path))) == ("bench-a", "hint")


def test_a_device_with_no_identity_of_its_own_is_never_located_by_port(tmp_path):
    state(tmp_path, "bench-a", ["10c4:ea60:0001"])
    assert location.identity(BRIDGE) == set()
    assert location.locate(BRIDGE, "bench-b", location.seen(str(tmp_path))) == ("bench-b", "hint")


def test_a_boards_other_usb_face_locates_it_too(tmp_path):
    """`match_also` (a stock app's face before the first flash) counts, and a MAC without
    colons is the same board as the MAC with them (what the relay files it under)."""
    two_faces = {"match": HELTEC["match"],
                 "match_also": [{"serial_by_id": "usb-Espressif_Systems_Some_Board_020000000010-if00"}]}
    assert location.identity(two_faces) == {"02:00:00:00:00:10"}
    bare = {"match": {"usb_serial": "020000000010"}}
    assert location.identity(bare) == {"02:00:00:00:00:10"}
    state(tmp_path, "bench-b", ["02:00:00:00:00:10"])
    assert location.locate(two_faces, None, location.seen(str(tmp_path))) == ("bench-b", "seen")


def test_a_bridge_with_a_serial_of_its_own_is_located_by_it(tmp_path):
    """A USB bridge given a serial on purpose (so two of a kind can be told apart) names its
    board: the relay keys it VID:PID:serial, and the registry's serial_by_id carries the
    serial. A factory serial (0001) still names nothing."""
    named = {"match": {"serial_by_id":
                       "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_node3-if00-port0"}}
    assert location.identity(named) == {"*:NODE3"}
    short = {"match": {"serial_by_id":
                       "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_n3-if00-port0"}}
    assert location.identity(short) == {"*:N3"}       # short, but a name
    state(tmp_path, "bench-a", ["10c4:ea60:node3", "10c4:ea60:0001"])
    idx = location.seen(str(tmp_path))
    assert location.locate(named, "bench-b", idx) == ("bench-a", "seen")
    assert location.locate(BRIDGE, "bench-b", idx) == ("bench-b", "hint")
    state(tmp_path, "bench-a", ["10c4:ea60:0001"])
    assert location.locate(named, "bench-b", location.seen(str(tmp_path))) == ("bench-b", "hint")


def test_a_bridge_on_its_factory_serial_is_named_as_such():
    """The remedy is a serial of the bridge's own; the warning that says so needs the serial."""
    assert location.factory_serial(BRIDGE) == "0001"
    named = {"match": {"serial_by_id":
                       "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_node3-if00-port0"}}
    assert location.factory_serial(named) is None
    assert location.factory_serial({"match": {"usb_serial": "0123456789ABCDEF"}}) is None
    assert location.factory_serial({}) is None


def test_a_board_the_relay_saw_once_but_has_not_settled_counts_as_seen(tmp_path):
    # just reflashed: gone from `reported`, back in the latest run (`pending`)
    (tmp_path / "plug-bench-b.json").write_text(json.dumps(
        {"reported": [], "pending": ["1A1B1C1D1E1F2021"]}))
    assert location.locate(XIAO, "bench-a", location.seen(str(tmp_path))) == ("bench-b", "seen")


def test_a_bench_host_that_is_this_machine_is_read_live_from_sysfs(tmp_path):
    def dev(name, vid, pid, serial=None, cls="00"):
        d = tmp_path / name
        d.mkdir()
        for k, v in (("idVendor", vid), ("idProduct", pid), ("bDeviceClass", cls)) + (
                (("serial", serial),) if serial else ()):
            (d / k).write_text(v + "\n")
    dev("1-1", "2109", "3431", cls="09")                       # a hub: not a board
    dev("1-1.3", "2fe3", "0004", "1A1B1C1D1E1F2021")           # a board, keyed by chip id
    dev("1-1.4", "04d9", "0007")                               # no serial: keyed by port
    idx = location.seen_here({"pi1"}, sysfs=str(tmp_path))
    assert idx == {"1A1B1C1D1E1F2021": "pi1", "04D9:0007:PORT-1-1.4": "pi1"}
    assert location.locate(XIAO, "bench-a", idx) == ("pi1", "seen")
    assert location.seen_here(set(), sysfs=str(tmp_path)) == {}


# ---- a bootloader that hides the chip id (docs/ARCHITECTURE.md#plug-watcher-and-busy-marks) -------------

POG = {"match": {"usb_serial": "0A0B0C0D0E0F1011"}}
ROM = {"busid": "1-1.2.1", "usb_vid": "2e8a", "usb_pid": "0003", "serial": "A1A2A3A4A5A6"}


def test_a_bootloader_the_watcher_identified_is_keyed_as_its_board():
    assert location.device_key({**ROM, "serial_is_id": False, "chip_id": "0a0b0c0d0e0f1011"}) \
        == "0A0B0C0D0E0F1011"


def test_a_hiding_bootloaders_own_serial_is_never_read_as_a_mac():
    assert location.device_key(ROM) == "A1:A2:A3:A4:A5:A6", "today's misreading, without the flag"
    assert location.device_key({**ROM, "serial_is_id": False}) == "2e8a:0003:A1A2A3A4A5A6"


def test_the_live_read_takes_the_watchers_answer_for_the_same_enumeration(tmp_path):
    sysfs = tmp_path / "sys"
    d = sysfs / "1-1.2.1"
    d.mkdir(parents=True)
    for k, v in (("idVendor", "2e8a"), ("idProduct", "0003"), ("bDeviceClass", "00"),
                 ("serial", "A1A2A3A4A5A6"), ("devnum", "92")):
        (d / k).write_text(v + "\n")
    snap = tmp_path / "usb-present.json"
    snap.write_text(json.dumps({"devices": [{**ROM, "devnum": "92", "serial_is_id": False,
                                             "chip_id": "0A0B0C0D0E0F1011"}]}))
    idx = location.seen_here({"pi1"}, sysfs=str(sysfs), snapshot=str(snap))
    assert idx == {"0A0B0C0D0E0F1011": "pi1"}
    assert location.locate(POG, None, idx) == ("pi1", "seen")
    # another enumeration (replugged since): the old answer is not carried over
    snap.write_text(json.dumps({"devices": [{**ROM, "devnum": "91", "serial_is_id": False,
                                             "chip_id": "0A0B0C0D0E0F1011"}]}))
    idx = location.seen_here({"pi1"}, sysfs=str(sysfs), snapshot=str(snap))
    assert "0A0B0C0D0E0F1011" not in idx and location.locate(POG, None, idx) == (None, "hint")


def test_a_twelve_digit_serial_with_its_vid_and_pid_is_also_known_as_the_relay_files_it():
    """An RP2040's ROM bootloader has a twelve-hex-digit serial, which is no MAC: the relay files
    it VID:PID:serial, so a board registered by it is seen; an ESP32's MAC serial still matches."""
    rp = {"match": {"usb_vid": "2e8a", "usb_pid": "0003", "usb_serial": "1A1B1C1D1E1F"}}
    assert location.identity(rp) == {"1A:1B:1C:1D:1E:1F", "2E8A:0003:1A1B1C1D1E1F"}
    assert location.locate(rp, None, index={"2E8A:0003:1A1B1C1D1E1F": "pi1"}) == ("pi1", "seen")
    esp = {"match": {"usb_serial": "020000000010"}}
    assert location.identity(esp) == {"02:00:00:00:00:10"}
