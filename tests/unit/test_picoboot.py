"""The picoboot flasher (RP2040/RP2350 through the ROM bootloader), against a fake picotool that
keeps the chip's flash in a file and whether it is in its bootloader in another."""
import json
import os
import stat
import struct
import sys

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, picoboot

CHIP = "1B2B3B4B5B6B7B8B"
FAMILY = 0xE48BFF56
SPEC = {"family": FAMILY, "app": [0x10000000, 0x11000000], "enter": ["reset-interface"]}
BASE = 0x10000000


def uf2(path, payload, addr=BASE, family=FAMILY, size=256):
    """A UF2 file of `payload` from `addr`, in blocks of `size` bytes."""
    chunks = [payload[i:i + size] for i in range(0, len(payload), size)]
    with open(path, "wb") as f:
        for n, c in enumerate(chunks):
            head = struct.pack("<8I", 0x0A324655, 0x9E5D5157, 0x2000, addr + n * size, len(c),
                               n, len(chunks), family)
            f.write(head + c.ljust(476, b"\0") + struct.pack("<I", 0x0AB16F30))
    return str(path)


FAKE = r'''#!/usr/bin/env python3
"""picotool, as far as the flasher uses it: info -d, save -r, load -v, reboot [-f -u]."""
import json, os, sys, struct
st = os.environ["FAKE_PT"]
state = json.load(open(st))
args = sys.argv[1:]
ser = args[args.index("--ser") + 1] if "--ser" in args else None
log = open(st + ".log", "a"); log.write(" ".join(args) + "\n")
flash = bytearray(open(state["flash"], "rb").read())
def save(): open(state["flash"], "wb").write(flash); json.dump(state, open(st, "w"))
# picotool 2.1 exits 0 when it finds no device: only its words say so
if ser != state["chip"]:
    print("No accessible RP-series devices in BOOTSEL mode were found with serial number", ser); sys.exit(0)
cmd = args[0]
if cmd == "reboot":
    if "-u" in args:
        if not state.get("reset_interface"):
            print("No accessible RP-series devices in BOOTSEL mode were found"); sys.exit(1)
        state["boot"] = True
    else:
        state["boot"] = False
    save(); print("The device was rebooted"); sys.exit(0)
if not state["boot"]:
    print("No accessible RP-series devices in BOOTSEL mode were found"); sys.exit(0)
if cmd == "info":
    if state.get("rp2350"):
        print(f"Device Information\n type: RP2350\n revision: A2\n chipid: 0x{state['flash_id'].lower()}")
    else:
        # a blank flash: picotool gives the flash id and size with an image's information only
        blank = not flash.strip(b"\xff")
        print("Device Information\n type: RP2040\n revision: B2" + ("" if blank else
              f"\n flash size: {len(flash)//1024}K\n flash id: 0x{state['flash_id']}"))
elif cmd == "save":
    lo, hi, out = int(args[2], 16), int(args[3], 16), args[4]
    if os.path.splitext(out)[1] not in (".bin", ".uf2", ".elf"):
        print(f"ERROR: filename '{out}' does not have a recognized file type (extension)"); sys.exit(1)
    open(out, "wb").write(bytes(flash[lo - 0x10000000:hi - 0x10000000]))
elif cmd == "erase":
    lo, hi = int(args[2], 16), int(args[3], 16)
    flash[lo - 0x10000000:hi - 0x10000000] = b"\xff" * (hi - lo)
    save(); print("Erasing: [==] 100%")
elif cmd == "load":
    data = open(args[2], "rb").read()
    for i in range(0, len(data), 512):
        _, _, flags, addr, size = struct.unpack_from("<5I", data, i)
        flash[addr - 0x10000000:addr - 0x10000000 + size] = data[i + 32:i + 32 + size]
    if state.get("load_fails"):
        print("ERROR: load failed"); sys.exit(1)
    save(); print("Loading into Flash: [==] 100%\nVerifying Flash: OK")
else:
    sys.exit(2)
'''


@pytest.fixture
def chip(tmp_path, monkeypatch):
    flash = tmp_path / "flash.bin"
    flash.write_bytes(b"\xff" * 0x40000)
    st = tmp_path / "state.json"
    st.write_text(json.dumps({"chip": CHIP, "flash_id": CHIP, "flash": str(flash), "boot": False,
                              "reset_interface": True}))
    tool = tmp_path / "picotool"
    tool.write_text(FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable))
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_PT", str(st))

    def set_state(**kw):
        s = json.loads(st.read_text())
        s.update(kw)
        st.write_text(json.dumps(s))

    def log():
        p = tmp_path / "state.json.log"
        return p.read_text().splitlines() if p.exists() else []

    return tmp_path, str(tool), set_state, log, flash


def image(tmp_path, payload, **kw):
    d = tmp_path / "img"
    d.mkdir(exist_ok=True)
    f = uf2(d / "app.uf2", payload, **kw)
    blocks, _ = picoboot.uf2_blocks(f)
    manifest = {"family": "bare", "format": "uf2", "file": "app.uf2",
                "board_tag": "pro-micro", "hash": picoboot.payload_hash(blocks)}
    (d / "manifest.json").write_text(json.dumps(manifest))
    return common.load_image(str(d))


def flasher(tool, manifest, file, **kw):
    return picoboot.PicobootFlasher("pog1", manifest, file, CHIP, kw.pop("spec", SPEC),
                                    board_tag="pro-micro", picotool=tool,
                                    enter_wait=kw.pop("enter_wait", 1.0), **kw)


def test_a_payload_hashes_the_same_from_the_file_and_from_a_flash_holding_it(tmp_path):
    payload = os.urandom(1000)
    blocks, fams = picoboot.uf2_blocks(uf2(tmp_path / "a.uf2", payload))
    assert fams == {FAMILY} and picoboot.ranges(blocks) == [(BASE, BASE + 1000)]
    flash = b"\x00" * 16 + payload + b"\x00" * 16
    read = lambda lo, hi: flash[lo - BASE + 16:hi - BASE + 16]  # noqa: E731
    assert picoboot.board_hash(blocks, read) == picoboot.payload_hash(blocks)


def test_refusals_name_the_wrong_family_and_a_write_outside_the_region(tmp_path):
    blocks, fams = picoboot.uf2_blocks(uf2(tmp_path / "a.uf2", b"x" * 300, family=0xADA52840))
    assert any("UF2 family" in r for r in picoboot.refusals(blocks, fams, SPEC))
    blocks, fams = picoboot.uf2_blocks(uf2(tmp_path / "b.uf2", b"x" * 300, addr=0x20000000))
    assert any("outside" in r for r in picoboot.refusals(blocks, fams, SPEC))
    blocks, fams = picoboot.uf2_blocks(uf2(tmp_path / "c.uf2", b"x" * 300))
    assert picoboot.refusals(blocks, fams, SPEC) == []


def test_not_a_uf2_is_refused(tmp_path):
    p = tmp_path / "x.uf2"
    p.write_bytes(b"\0" * 512)
    with pytest.raises(picoboot.Uf2Error):
        picoboot.uf2_blocks(str(p))


def test_a_plan_reads_the_board_and_puts_it_back_in_its_application(chip):
    tmp, tool, _, log, _ = chip
    manifest, file = image(tmp, os.urandom(700))
    f = flasher(tool, manifest, file)
    try:
        r = common.drive(f, manifest, check_mode=True)
    finally:
        f.leave()
    assert r["changed"] and r["before"]["hash"] != manifest["hash"]
    calls = [c.split()[0] + (" -u" if " -u" in c else "") for c in log()]
    assert calls[0] == "info" and "reboot -u" in calls and calls[-1] == "reboot"
    assert not any(c.startswith("load") for c in log())


def test_an_apply_writes_reads_back_and_a_second_run_changes_nothing(chip):
    tmp, tool, _, _, flash = chip
    payload = os.urandom(900)
    manifest, file = image(tmp, payload)
    f = flasher(tool, manifest, file)
    try:
        r = common.drive(f, manifest)
    finally:
        f.leave()
    assert r["changed"] and r["after"]["hash"] == manifest["hash"]
    assert flash.read_bytes()[:900] == payload
    assert json.loads((tmp / "state.json").read_text())["boot"] is False   # back in its app
    f = flasher(tool, manifest, file)
    try:
        assert common.drive(f, manifest)["changed"] is False
    finally:
        f.leave()


def test_an_rp2350_is_known_by_its_own_chip_id(chip):
    tmp, tool, set_state, _, _ = chip
    set_state(boot=True, rp2350=True)
    manifest, file = image(tmp, b"y" * 300)
    f = flasher(tool, manifest, file)
    assert f.in_bootloader() and f.chip_facts["chip_id"] == CHIP and f.chip_facts["type"] == "RP2350"


def test_another_chip_in_its_bootloader_is_refused(chip):
    tmp, tool, set_state, _, flash = chip
    flash.write_bytes(b"\x00" * 0x1000 + b"\xff" * (0x40000 - 0x1000))   # an image: picotool prints the id
    set_state(boot=True, flash_id="0123456789ABCDEF")
    manifest, file = image(tmp, b"y" * 300)
    with pytest.raises(common.FlashError, match="REFUSING"):
        flasher(tool, manifest, file).device()


def test_a_board_no_way_reaches_is_unanswered_and_names_what_was_tried(chip):
    tmp, tool, set_state, _, _ = chip
    set_state(reset_interface=False)
    manifest, file = image(tmp, b"y" * 300)
    with pytest.raises(common.Unanswered, match="reset-interface"):
        flasher(tool, manifest, file, enter=["reset-interface", "manual"]).device()


def test_a_failed_load_fails_the_flash(chip):
    tmp, tool, set_state, _, _ = chip
    set_state(load_fails=True)
    manifest, file = image(tmp, b"z" * 300)
    f = flasher(tool, manifest, file)
    with pytest.raises(common.FlashError, match="load failed"):
        try:
            common.drive(f, manifest)
        finally:
            f.leave()


def test_a_port_that_takes_no_input_fails_its_ask_instead_of_hanging(chip, monkeypatch):
    tmp, tool, _, _, _ = chip
    manifest, file = image(tmp, b"y" * 300)
    f = flasher(tool, manifest, file)
    f.ask_timeout = 1
    monkeypatch.setattr(f, "_ASK", "import time; time.sleep(30)")
    with pytest.raises(common.FlashError, match="took no input within 1 s"):
        f._shell("/dev/null", "app bootsel")


def test_the_waits_are_the_layouts_then_the_board_types_then_the_defaults(chip):
    tmp, tool, _, _, _ = chip
    manifest, file = image(tmp, b"y" * 300)
    mk = lambda spec, **kw: picoboot.PicobootFlasher("pog1", manifest, file, CHIP, spec,  # noqa: E731
                                                     picotool=tool, **kw)
    f = mk(SPEC)
    assert (f.enter_wait, f.ask_timeout) == (picoboot.ENTER_WAIT, picoboot.ASK_TIMEOUT)
    f = mk({**SPEC, "enter_wait": 30, "ask_timeout": 40})
    assert (f.enter_wait, f.ask_timeout) == (30.0, 40.0)
    f = mk({**SPEC, "enter_wait": 30, "ask_timeout": 40}, enter_wait=5, ask_timeout=6)
    assert (f.enter_wait, f.ask_timeout) == (5.0, 6.0)



def test_a_blank_rp2040_is_known_by_the_id_picotool_selected_it_by(chip):
    """After a whole-flash erase picotool prints no flash id (seen on a KB2040): the board it
    found with `--ser <flash id>` is that chip (picotool reads the flash id itself to match)."""
    tmp, tool, set_state, _, flash = chip
    flash.write_bytes(b"\xff" * 0x40000)
    set_state(boot=True)
    manifest, file = image(tmp, b"y" * 300)
    f = flasher(tool, manifest, file)
    assert f.in_bootloader() and f.chip_facts["chip_id"] == CHIP and "flash_size" not in f.chip_facts


def test_nothing_found_is_not_a_board_in_its_bootloader_though_picotool_exits_0(chip):
    tmp, tool, set_state, _, _ = chip
    set_state(boot=False)
    manifest, file = image(tmp, b"y" * 300)
    f = flasher(tool, manifest, file)
    assert f.in_bootloader() is False
    rc, out = f._pt("load", "-v", file)
    assert rc != 0, "a load that found no device is not a success"


def test_an_image_whose_shell_is_not_on_the_usb_port_is_refused_for_a_board_reached_by_it(tmp_path):
    """An image whose console is a UART is refused for a board reached by USB: written, it answers
    nowhere and only BOOTSEL brings it back."""
    manifest, file = image(tmp_path, os.urandom(300))
    manifest["console"] = "uart"
    f = flasher("picotool", manifest, file)
    f.console_expected = True
    assert any("not the board's USB port" in r for r in f.preflight())
    f.console_elsewhere = True
    assert not any("USB port" in r for r in f.preflight())
    f.console_elsewhere, manifest["console"] = False, "usb"
    assert not any("USB port" in r for r in f.preflight())
    f.console_expected, manifest["console"] = False, "uart"   # a board with no console of its own
    assert not any("USB port" in r for r in f.preflight())


def test_a_long_range_is_read_in_pieces_and_joined_byte_for_byte(chip, monkeypatch):
    # picotool's save slows down as its range grows: a range is read in pieces
    tmp, tool, set_state, log, flash = chip
    data = os.urandom(0x40000)
    flash.write_bytes(data)
    set_state(boot=True)
    monkeypatch.setattr(picoboot, "SAVE_PIECE", 0x10000)
    rom = picoboot.Bootrom("pog1", CHIP, SPEC, picotool=tool)
    out = tmp / "out.bin"
    rom.save(BASE + 0x100, BASE + 0x30000, str(out))
    assert out.read_bytes() == data[0x100:0x30000]
    saves = [c.split()[2:4] for c in log() if c.startswith("save")]
    assert saves[0] == [f"0x{BASE + 0x100:x}", f"0x{BASE + 0x10100:x}"]
    assert saves[-1][1] == f"0x{BASE + 0x30000:x}" and len(saves) == 3
    assert not list(tmp.glob("*.piece.bin"))


def test_a_piece_that_fails_or_comes_back_short_stops_the_read(chip, monkeypatch):
    tmp, tool, set_state, _, flash = chip
    set_state(boot=True)
    monkeypatch.setattr(picoboot, "SAVE_PIECE", 0x10000)
    rom = picoboot.Bootrom("pog1", CHIP, SPEC, picotool=tool)
    with pytest.raises(common.FlashError, match="gave"):    # past the end of the flash
        rom.save(BASE + 0x30000, BASE + 0x50000, str(tmp / "short.bin"))
    set_state(boot=False)
    with pytest.raises(common.FlashError, match="failed"):
        rom.save(BASE, BASE + 0x20000, str(tmp / "none.bin"))
    assert not list(tmp.glob("*.piece.bin"))
    assert not list(tmp.glob("short.bin*")) and not list(tmp.glob("none.bin*")), \
        "a read that failed leaves nothing under the backup's name, nor a .part"


def test_a_piece_whose_save_exits_0_but_writes_nothing_is_not_the_last_piece_again(chip, monkeypatch):
    """A stale piece of the same length would pass silently as the next range's bytes."""
    tmp, tool, set_state, _, flash = chip
    flash.write_bytes(os.urandom(0x40000))
    set_state(boot=True)
    monkeypatch.setattr(picoboot, "SAVE_PIECE", 0x10000)
    rom = picoboot.Bootrom("pog1", CHIP, SPEC, picotool=tool)
    real, calls = rom._pt, []                              # pylint: disable=protected-access

    def pt(*args, **kw):
        calls.append(args[0])
        if args[0] == "save" and calls.count("save") == 2:
            return 0, ""                                    # rc 0, no file written
        return real(*args, **kw)
    monkeypatch.setattr(rom, "_pt", pt)
    out = tmp / "chip.bin"
    with pytest.raises(common.FlashError, match="wrote no file"):
        rom.save(BASE, BASE + 0x30000, str(out))
    assert not out.exists() and not list(tmp.glob("chip.bin*"))


def test_an_application_on_its_way_back_from_the_bootloader_is_waited_for(chip, monkeypatch):
    """A board that left its bootloader a moment ago is not on USB yet: absent is waited for
    (a Pico 2 W on zapp 3.0 takes about 4 s), an ambiguous match is not."""
    tmp, tool, _, _, _ = chip
    manifest, file = image(tmp, b"y" * 300)
    f = flasher(tool, manifest, file)
    answers = [(None, common.Absent("devreg cannot resolve pog1: NOT PRESENT"))] * 2 + [("/dev/ttyACM0", None)]
    monkeypatch.setattr(common, "guarded_port", lambda devreg, name: answers.pop(0))
    monkeypatch.setattr(picoboot.time, "sleep", lambda s: None)
    assert f._app_port(wait=5) == "/dev/ttyACM0" and answers == []
    monkeypatch.setattr(common, "guarded_port", lambda devreg, name: (None, "devreg cannot resolve pog1: matched 2 devices"))
    with pytest.raises(common.FlashError, match="matched 2"):
        f._app_port(wait=5)


def test_an_application_that_never_comes_back_is_absent_once_the_wait_is_over(chip, monkeypatch):
    tmp, tool, _, _, _ = chip
    manifest, file = image(tmp, b"y" * 300)
    f = flasher(tool, manifest, file)
    calls = []
    monkeypatch.setattr(common, "guarded_port", lambda devreg, name: calls.append(1) or
                        (None, common.Absent("devreg cannot resolve pog1: NOT PRESENT")))
    with pytest.raises(common.FlashError, match="NOT PRESENT"):
        f._app_port(wait=0)
    assert len(calls) == 1
