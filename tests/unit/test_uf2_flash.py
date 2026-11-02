"""uf2_flash.py: what a board type's catalog entry decides (which USB device is a bootloader,
which addresses and families an image may carry, how a drive names its chip), how a board's
type is found, and the drive guard for two board types, against a fake sysfs."""
import importlib.util
import os
import struct

import pytest
import yaml

os.environ["UF2_FLASH_REEXEC"] = "1"      # importing must not re-exec under another python
REPO = os.path.join(os.path.dirname(__file__), "..", "..")
SPEC = importlib.util.spec_from_file_location(
    "uf2_flash", os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "bin", "uf2_flash.py"))
uf2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uf2)

CHIP_A, CHIP_B = "0123456789ABCDEF", "FEDCBA9876543210"


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    """A bench host: the tool's own board catalog, and a registry with a XIAO whose type the
    registry does not say, a T114 whose type it does, and a board of a type without UF2."""
    reg = tmp_path / "registry.yaml"
    reg.write_text(yaml.safe_dump({"devices": {
        "kitA": {"match": {"usb_serial": CHIP_A}, "aliases": ["a"]},
        "t1": {"match": {"usb_serial": CHIP_B}, "policy": {"board_tag": "heltec-t114"}},
        "esp": {"match": {"usb_serial": "A" * 16}, "policy": {"board_tag": "heltec-v4"}},
        "bridge": {"match": {"usb_serial": "bridge-1"}},
    }}))
    monkeypatch.setattr(uf2, "REGISTRY", str(reg))
    monkeypatch.setattr(uf2, "BOARDS", [os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "mesh", "catalog", "boards.yml")])
    monkeypatch.setattr(uf2, "BOARD_TYPE", None)
    monkeypatch.setattr(uf2, "_YAML", {})
    return tmp_path


def fake_usb(root, cls, node, vid, pid, serial, port="1-1.2"):
    """A sysfs with one USB device and, under it, the block device or tty `node`."""
    usb = root / "devices" / "pci0" / "usb1" / port
    leaf = usb / f"{port}:1.0" / cls / node
    leaf.mkdir(parents=True)
    for name, value in (("idVendor", vid), ("idProduct", pid), ("serial", serial)):
        (usb / name).write_text(value + "\n")
    (root / "class" / cls).mkdir(parents=True, exist_ok=True)
    os.symlink(leaf, root / "class" / cls / node)
    return str(root)


def image(lo, hi, family=0xADA52840):
    blocks = []
    for addr in range(lo, hi, 256):
        blocks.append(struct.pack("<8I", 0x0A324655, 0x9E5D5157, 0x2000, addr, 256, 0, 0, family)
                      .ljust(508, b"\0") + struct.pack("<I", 0x0AB16F30))
    return b"".join(blocks)


# ---- board types ---------------------------------------------------------------------------

def test_the_catalog_gives_each_type_its_bootloader_and_its_app_region():
    t = uf2.board_types()
    assert set(t) == {"xiao", "heltec-t114"}, "only types with a uf2 block"
    assert t["xiao"]["bootloader_usb"] == {("2886", "0045")}
    assert (t["xiao"]["app_lo"], t["xiao"]["app_hi"]) == (0x27000, 0xF4000)
    assert t["heltec-t114"]["bootloader_usb"] == {("239a", "0071")}
    assert t["heltec-t114"]["app_lo"] == 0x26000


def test_refused_families_are_read_whatever_their_keys_went_through(host, monkeypatch):
    """The bench host's catalog is written through JSON (keys become decimal strings); a site
    may write a family as a hex string."""
    site = host / "site-boards.yml"
    site.write_text(yaml.safe_dump({"boards": {"kit-c": {"uf2": {
        "bootloader_usb": ["1209:0001"], "app": [0x1000, 0x2000], "family": 1,
        "refuse_families": {"3596845628": "an update", "0x4D455348": "an erase", 7: "a test"}}}}}))
    monkeypatch.setattr(uf2, "BOARDS", uf2.BOARDS + [str(site)])
    assert uf2.board_types()["kit-c"]["refuse_families"] == {
        0xD663823C: "an update", 0x4D455348: "an erase", 7: "a test"}


def test_a_sites_catalog_is_layered_over_the_tools(host, monkeypatch):
    site = host / "site-boards.yml"
    site.write_text(yaml.safe_dump({"boards": {"rak4631": {"mcu": "nrf52840", "uf2": {
        "bootloader_usb": ["239A:0029"], "app": [0x26000, 0xF4000], "family": 0xADA52840}}}}))
    monkeypatch.setattr(uf2, "BOARDS", uf2.BOARDS + [str(site), str(host / "absent.yml")])
    assert uf2.board_types()["rak4631"]["bootloader_usb"] == {("239a", "0029")}
    assert "xiao" in uf2.board_types()


def test_a_uf2_block_that_is_not_whole_is_an_error(host, monkeypatch):
    bad = host / "bad.yml"
    bad.write_text(yaml.safe_dump({"boards": {"odd": {"uf2": {"bootloader_usb": ["2886:0045"]}}}}))
    monkeypatch.setattr(uf2, "BOARDS", [str(bad)])
    with pytest.raises(SystemExit, match="board type 'odd'"):
        uf2.board_types()


def test_a_boards_type_is_the_registrys_or_the_callers_and_they_must_agree(monkeypatch):
    assert uf2.profile("t1")["type"] == "heltec-t114"
    with pytest.raises(SystemExit, match="board type is not known.*give --type"):
        uf2.profile("kitA")
    with pytest.raises(SystemExit, match="'heltec-v4' has no uf2 block"):
        uf2.profile("esp")
    monkeypatch.setattr(uf2, "BOARD_TYPE", "xiao")
    assert uf2.profile("kitA")["type"] == "xiao" and uf2.profile("a")["type"] == "xiao"
    with pytest.raises(SystemExit, match="'heltec-t114' in the registry, the caller says 'xiao'"):
        uf2.profile("t1")
    with pytest.raises(SystemExit, match="unknown board 'nobody'"):
        uf2.profile("nobody")
    with pytest.raises(SystemExit, match="unknown board 'bridge'"):
        uf2.profile("bridge")                       # not named by a chip id


# ---- which port is a bootloader --------------------------------------------------------------

def test_a_bootloader_is_told_from_an_app_by_its_usb_ids_not_by_its_name(host):
    """A T114's app and bootloader carry the same product string: only the ids differ."""
    t114 = uf2.board_types()["heltec-t114"]
    by_id = host / "by-id"
    by_id.mkdir()
    link = by_id / f"usb-Heltec_HT-n5262_{CHIP_B}-if00"
    sysfs = fake_usb(host / "app", "tty", "ttyACM3", "239a", "8029", CHIP_B)
    os.symlink("/dev/ttyACM3", link)
    assert uf2.ports_for(CHIP_B, t114, str(by_id), sysfs) == (str(link), None)
    sysfs = fake_usb(host / "boot", "tty", "ttyACM3", "239a", "0071", CHIP_B)
    assert uf2.ports_for(CHIP_B, t114, str(by_id), sysfs) == (None, str(link))
    # the same device read as a XIAO is no bootloader, and another chip's port is not this board's
    assert uf2.ports_for(CHIP_B, uf2.board_types()["xiao"], str(by_id), sysfs) == (str(link), None)
    assert uf2.ports_for(CHIP_A, t114, str(by_id), sysfs) == (None, None)


# ---- the image -----------------------------------------------------------------------------

def test_an_image_is_checked_against_its_board_types_app_region(host):
    types = uf2.board_types()
    f = host / "app.uf2"
    f.write_bytes(image(0x26000, 0x27000))
    uf2.check_uf2(str(f), types["heltec-t114"])
    with pytest.raises(SystemExit, match="outside the xiao app region 0x27000..0xf4000"):
        uf2.check_uf2(str(f), types["xiao"])
    f.write_bytes(image(0xF3F00, 0xF4100))
    with pytest.raises(SystemExit, match="outside the heltec-t114 app region"):
        uf2.check_uf2(str(f), types["heltec-t114"])
    for fam, what in ((0xD663823C, "a bootloader update"), (0x4D455348, "an OTAFIX factory erase")):
        f.write_bytes(image(0x27000, 0x27100, family=fam))
        with pytest.raises(SystemExit, match=what):
            uf2.check_uf2(str(f), types["xiao"])
    f.write_bytes(image(0x27000, 0x27100, family=0x12345678))
    with pytest.raises(SystemExit, match="is not a xiao app family"):
        uf2.check_uf2(str(f), types["xiao"])
    f.write_bytes(b"not a uf2")
    with pytest.raises(SystemExit, match="is not a UF2 file"):
        uf2.check_uf2(str(f), types["xiao"])


# ---- the drive guard, per board type ---------------------------------------------------------

@pytest.fixture
def drive(tmp_path, monkeypatch):
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    (mnt / "INFO_UF2.TXT").write_text("UF2 Bootloader 0.9.2-OTAFIX2.5\r\n")
    monkeypatch.setattr(uf2, "mount_source", lambda _m: "/dev/sdb")
    return mnt


@pytest.mark.parametrize("tag, vid, pid", [("xiao", "2886", "0045"), ("heltec-t114", "239a", "0071")])
def test_the_right_boards_drive_passes_and_anothers_is_refused(tmp_path, drive, tag, vid, pid):
    prof = uf2.board_types()[tag]
    sysfs = fake_usb(tmp_path / "sys", "block", "sdb", vid, pid, CHIP_A)
    got = uf2.check_drive("kitA", CHIP_A.lower(), str(drive), prof, sysfs=sysfs)
    assert got["confirmed_by"] == "usb-serial" and got["usb"] == f"{vid}:{pid}"
    with pytest.raises(SystemExit, match="not t1"):
        uf2.check_drive("t1", CHIP_B, str(drive), prof, sysfs=sysfs)
    with pytest.raises(SystemExit, match="not the one that was checked first"):
        uf2.check_drive("kitA", CHIP_A, str(drive), prof, expect_st_dev=got["st_dev"] + 1,
                        sysfs=sysfs)


def test_a_drive_of_another_board_type_is_refused(tmp_path, drive):
    """A XIAO's bootloader drive is not a T114's, even with the right serial; nor is an app."""
    types = uf2.board_types()
    sysfs = fake_usb(tmp_path / "sys", "block", "sdb", "2886", "0045", CHIP_A)
    with pytest.raises(SystemExit, match="not a heltec-t114 UF2 bootloader .239a:0071."):
        uf2.check_drive("kitA", CHIP_A, str(drive), types["heltec-t114"], sysfs=sysfs)
    sysfs = fake_usb(tmp_path / "sys2", "block", "sdb", "2fe3", "0004", CHIP_A)
    with pytest.raises(SystemExit, match="not a xiao UF2 bootloader"):
        uf2.check_drive("kitA", CHIP_A, str(drive), types["xiao"], sysfs=sysfs)


def test_a_drive_that_names_its_own_chip_is_trusted_over_the_lookup(tmp_path, drive):
    prof = uf2.board_types()["xiao"]
    sysfs = fake_usb(tmp_path / "sys", "block", "sdb", "2886", "0045", CHIP_A)
    (drive / "NODEINFO.TXT").write_text(f"Chip-ID: {CHIP_A}\r\n")
    assert uf2.check_drive("kitA", CHIP_A, str(drive), prof, sysfs=sysfs)["confirmed_by"] == "NODEINFO.TXT"
    (drive / "NODEINFO.TXT").write_text(f"Chip-ID: {CHIP_B}\r\n")
    with pytest.raises(SystemExit, match="the drive says it is chip"):
        uf2.check_drive("kitA", CHIP_A, str(drive), prof, sysfs=sysfs)
    os.remove(drive / "INFO_UF2.TXT")
    with pytest.raises(SystemExit, match="no INFO_UF2.TXT"):
        uf2.check_drive("kitA", CHIP_A, str(drive), prof, sysfs=sysfs)


def test_only_a_type_that_says_where_its_drive_names_the_chip_is_held_to_it(tmp_path, drive):
    """The identity file is the type's to name: a T114's bootloader names none, so a file of
    that name on its drive is nobody's word."""
    sysfs = fake_usb(tmp_path / "sys", "block", "sdb", "239a", "0071", CHIP_A)
    (drive / "NODEINFO.TXT").write_text(f"Chip-ID: {CHIP_B}\r\n")
    got = uf2.check_drive("kitA", CHIP_A, str(drive), uf2.board_types()["heltec-t114"], sysfs=sysfs)
    assert got["confirmed_by"] == "usb-serial"


@pytest.mark.parametrize("bad", [{"file": "ID.TXT", "pattern": "no group"},
                                 {"file": "a/ID.TXT", "pattern": "(x)"}, {"pattern": "(x)"}])
def test_a_drive_identity_that_cannot_name_a_chip_is_refused(host, monkeypatch, bad):
    site = host / "site-boards.yml"
    site.write_text(yaml.safe_dump({"boards": {"kit-c": {"uf2": {
        "bootloader_usb": ["1209:0001"], "app": [0x1000, 0x2000], "family": 1,
        "drive_identity": bad}}}}))
    monkeypatch.setattr(uf2, "BOARDS", uf2.BOARDS + [str(site)])
    with pytest.raises(SystemExit, match="drive_identity"):
        uf2.board_types()


def test_the_usb_device_behind_a_block_device_is_read_from_sysfs(tmp_path):
    sysfs = fake_usb(tmp_path / "sys", "block", "sdb", "2886", "0045", CHIP_A)
    assert uf2.usb_identity("/dev/sdb", sysfs) == ("2886", "0045", CHIP_A)
    assert uf2.usb_identity("/dev/nosuchdisk", sysfs) is None


def test_a_disk_that_is_not_on_usb_is_refused(tmp_path, drive):
    sysfs = tmp_path / "sys"
    disk = sysfs / "devices" / "platform" / "sda"
    disk.mkdir(parents=True)
    (sysfs / "class" / "block").mkdir(parents=True)
    os.symlink(disk, sysfs / "class" / "block" / "sdb")
    with pytest.raises(SystemExit, match="not a USB device"):
        uf2.check_drive("kitA", CHIP_A, str(drive), uf2.board_types()["xiao"], sysfs=str(sysfs))


# ---- one run per board -----------------------------------------------------------------------

def test_a_second_run_is_kept_off_a_board_one_is_working_on(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVLOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("UF2_LOCK_HELD", raising=False)
    held = uf2.board_lock(CHIP_A)
    assert held is not None and os.path.basename(held.name) == f"uf2-{CHIP_A.lower()}.lock"
    with pytest.raises(SystemExit, match="already working on"):
        uf2.board_lock(CHIP_A)
    assert uf2.board_lock(CHIP_B) is not None
    monkeypatch.setenv("UF2_LOCK_HELD", CHIP_A.lower())         # a parent that holds it
    assert uf2.board_lock(CHIP_A) is None


# ---- reaching UF2 mode -----------------------------------------------------------------------

def test_a_board_already_showing_its_drive_is_used_as_it_is(monkeypatch):
    monkeypatch.setattr(uf2, "resolve", lambda n: (CHIP_B, None, "/dev/serial/by-id/boot"))
    monkeypatch.setattr(uf2, "uf2_disk", lambda s, wait=0, missing=None, max_bytes=0: ("/dev/sdz", f"wait={wait}"))
    monkeypatch.setenv("UF2_ENTER", "manual")
    assert uf2.enter_uf2_mode("t1") == ("/dev/sdz", "wait=5.0")


def test_manual_entry_asks_nobody_and_waits_for_the_drive(monkeypatch, capsys):
    monkeypatch.setattr(uf2, "resolve", lambda n: (CHIP_B, "/dev/serial/by-id/app", None))
    monkeypatch.setattr(uf2, "uf2_disk", lambda s, wait=0, missing=None, max_bytes=0: ("/dev/sdz", f"wait={wait}"))
    monkeypatch.setattr(uf2.subprocess, "run", lambda *a, **k: pytest.fail("nothing is run"))
    monkeypatch.setenv("UF2_ENTER", "manual")
    monkeypatch.setenv("UF2_MANUAL_WAIT", "12")
    assert uf2.enter_uf2_mode("t1") == ("/dev/sdz", "wait=12.0")
    assert "double-tap its reset button" in capsys.readouterr().out
    monkeypatch.setenv("UF2_ENTER", "guess")
    with pytest.raises(SystemExit, match="auto or manual"):
        uf2.enter_uf2_mode("t1")


# ---- what a board type says, instead of what an nRF52 does ------------------------------------

@pytest.fixture
def more_types(host, monkeypatch):
    """Two more types beside the tool's: an RP2040 (its ROM drive has a serial of its own) and a
    board whose bootloader brings up its drive on a 1200-baud touch (a SAMD board, say)."""
    extra = host / "site-boards.yml"
    extra.write_text(yaml.safe_dump({"boards": {
        "rp": {"uf2": {"bootloader_usb": ["2e8a:0003"], "app": [0x10000000, 0x11000000],
                       "family": 0xE48BFF56, "drive_serial": "none"}},
        "samd": {"uf2": {"bootloader_usb": ["239a:000b"], "app": [0x2000, 0x40000],
                         "family": 0x68ED2B88, "touch": "uf2", "drive_max_mb": 8,
                         "manual": "double-tap RESET"}}}}))
    monkeypatch.setattr(uf2, "BOARDS", uf2.BOARDS + [str(extra)])
    reg = yaml.safe_load(open(uf2.REGISTRY))
    reg["devices"]["rp1"] = {"match": {"usb_serial": "1B2B3B4B5B6B7B8B"}, "policy": {"board_tag": "rp"}}
    reg["devices"]["sd1"] = {"match": {"usb_serial": "9B7C3D2A51504B5A"}, "policy": {"board_tag": "samd"}}
    open(uf2.REGISTRY, "w").write(yaml.safe_dump(reg))
    monkeypatch.setattr(uf2, "_YAML", {})


def test_the_nrf52_types_say_their_touch_is_serial_dfu_by_nrfutil():
    t = uf2.board_types()["xiao"]
    assert (t["touch"], t["dfu"], t["drive_serial"], t["drive_max"]) == \
        ("dfu", "adafruit-nrfutil", "chip", 64 * 1024 * 1024)


def test_a_type_whose_drive_does_not_carry_the_serial_is_refused_before_anything(more_types, monkeypatch):
    monkeypatch.setattr(uf2, "resolve", lambda n: pytest.fail("nothing is asked of the board"))
    with pytest.raises(SystemExit, match="does not carry the board's serial.*picoboot"):
        uf2.enter_uf2_mode("rp1")


def test_a_touch_is_sent_only_where_the_type_says_what_it_does(more_types, monkeypatch):
    touched = []
    monkeypatch.setattr(uf2, "touch_port", touched.append)
    monkeypatch.setattr(uf2, "resolve", lambda n: ("9B7C3D2A51504B5A", "/dev/serial/by-id/app", None))
    monkeypatch.setattr(uf2, "usb_identity", lambda *a, **k: ("239a", "800b", "9B7C3D2A51504B5A"))
    monkeypatch.setattr(uf2, "uf2_disk", lambda s, wait=0, missing=None, max_bytes=0: ("/dev/sdz", max_bytes))
    assert uf2.enter_uf2_mode("sd1") == ("/dev/sdz", 8 * 1024 * 1024)
    assert touched == ["/dev/serial/by-id/app"]
    # a type that says nothing of a touch is never touched for serial DFU either
    with pytest.raises(SystemExit, match="not put into serial DFU"):
        uf2.do_touch("sd1")


def test_serial_dfu_needs_the_type_to_name_its_tool(more_types, monkeypatch, tmp_path):
    pkg = tmp_path / "app.zip"
    pkg.write_bytes(b"x")
    monkeypatch.setattr(uf2.sys, "argv", ["uf2_flash.py", "dfu", "sd1", str(pkg)])
    with pytest.raises(SystemExit, match="names no serial DFU tool"):
        uf2.main()


def test_manual_entry_says_what_the_type_says_to_do(more_types, monkeypatch, capsys):
    monkeypatch.setattr(uf2, "resolve", lambda n: ("9B7C3D2A51504B5A", "/dev/serial/by-id/app", None))
    monkeypatch.setattr(uf2, "uf2_disk", lambda s, wait=0, missing=None, max_bytes=0: ("/dev/sdz", wait))
    monkeypatch.setenv("UF2_ENTER", "manual")
    monkeypatch.setenv("UF2_MANUAL_WAIT", "3")
    assert uf2.enter_uf2_mode("sd1") == ("/dev/sdz", 3.0)
    assert "double-tap RESET now" in capsys.readouterr().out


ZEPHYR = ("2fe3", "0004", "0123456789ABCDEF")
ALPHA = {"family": "alpha", "shell_command": "alpha dfu", "shell_usb": [], "app_tools": []}
BRAVO = {"family": "bravo", "shell_command": "bravo uf2", "shell_usb": ["2fe3"], "app_tools": []}
STOCK = {"family": "stock", "shell_command": None, "shell_usb": [],
         "app_tools": ["/opt/cli --port {port} --enter-dfu"]}


def test_an_app_is_asked_on_its_shell_only_on_a_vendor_its_family_names():
    narrow = {**ALPHA, "shell_usb": ["1B4F", "2e8a:000a"]}
    for ident in (("1b4f", "0001"), ("2e8a", "000a")):
        assert uf2.enter_choice("k", ident, [narrow]) == ("shell", "alpha", "alpha dfu")
    with pytest.raises(SystemExit, match="nothing was given"):
        uf2.enter_choice("k", ("2e8a", "000b"), [narrow])
    assert uf2.enter_choice("k", ZEPHYR, [ALPHA])[2] == "alpha dfu", "Zephyr's vendor by default"


def test_two_firmwares_on_one_vendor_that_ask_differently_are_refused_with_the_ways_out():
    with pytest.raises(SystemExit) as e:
        uf2.enter_choice("k", ZEPHYR, [ALPHA, BRAVO, STOCK], exists=lambda p: True)
    msg = str(e.value)
    assert "REFUSING" in msg and "2fe3:0004" in msg and "Nothing was sent" in msg
    assert "alpha: `alpha dfu`" in msg and "bravo: `bravo uf2`" in msg
    assert "bench_uf2_enter_as" in msg and "UF2_ENTER=manual" in msg


def test_two_families_that_ask_alike_are_no_ambiguity():
    same = {**BRAVO, "family": "charlie", "shell_command": "alpha dfu"}
    assert uf2.enter_choice("k", ZEPHYR, [ALPHA, same]) == ("shell", "alpha/charlie", "alpha dfu")


def test_an_app_on_another_vendor_is_asked_by_a_tool_present_here_in_list_order():
    stock_app = ("239a", "810b", "0123456789ABCDEF")
    kind, fam, argv = uf2.enter_choice("k", stock_app, [ALPHA, STOCK], exists=lambda p: p == "/opt/cli")
    assert (kind, fam, argv[0]) == ("tool", "stock", "/opt/cli")
    with pytest.raises(SystemExit, match="nothing was given"):
        uf2.enter_choice("k", stock_app, [ALPHA, STOCK], exists=lambda p: False)


def test_a_board_read_as_a_family_that_says_nothing_of_uf2_mode_is_asked_by_nobody_else():
    with pytest.raises(SystemExit) as e:
        uf2.enter_choice("k", ZEPHYR, [], runs="quiet")
    assert "runs quiet" in str(e.value) and "another family's command is not sent" in str(e.value)
    assert "UF2_ENTER=manual" in str(e.value)


def test_the_way_chosen_is_said_with_the_family_it_came_from():
    assert uf2.say_choice("k", "/dev/ttyACM0", ("shell", "alpha", "alpha dfu")) == \
        "k: its shell -> `alpha dfu` (UF2 mode, alpha's) on /dev/ttyACM0"
    assert "(UF2 mode, stock's)" in uf2.say_choice("k", "/dev/x", ("tool", "stock", ["/opt/cli"]))


def test_the_entries_are_read_from_what_the_flasher_hands_over(monkeypatch):
    import json
    monkeypatch.setenv("UF2_ENTERS", json.dumps([ALPHA]))
    assert uf2.enters() == [ALPHA]
    monkeypatch.setenv("UF2_ENTERS", "{not json")
    with pytest.raises(SystemExit, match="not JSON"):
        uf2.enters()
    monkeypatch.delenv("UF2_ENTERS")
    assert uf2.enters() == []


@pytest.mark.parametrize("bad", [{"touch": "maybe"}, {"dfu": "nrfjprog"}, {"drive_serial": "mac"}])
def test_a_type_with_an_unknown_word_is_an_error(host, monkeypatch, bad):
    extra = host / "bad.yml"
    extra.write_text(yaml.safe_dump({"boards": {"x": {"uf2": {
        "bootloader_usb": ["1234:5678"], "app": [1, 2], "family": 1, **bad}}}}))
    monkeypatch.setattr(uf2, "BOARDS", [str(extra)])
    with pytest.raises(SystemExit, match="touch is dfu"):
        uf2.board_types()


def test_a_type_may_take_several_uf2_families(host, monkeypatch, tmp_path):
    extra = host / "rp2350.yml"
    extra.write_text(yaml.safe_dump({"boards": {"p2": {"uf2": {
        "bootloader_usb": ["2e8a:000f"], "app": [0x10000000, 0x10400000],
        "family": [0xE48BFF59, 0xE48BFF57]}}}}))
    monkeypatch.setattr(uf2, "BOARDS", uf2.BOARDS + [str(extra)])
    prof = uf2.board_types()["p2"]
    assert prof["family"] == {0xE48BFF59, 0xE48BFF57}
    assert uf2.board_types()["xiao"]["family"] == {0xADA52840}      # one id, as before
    f = tmp_path / "abs.uf2"
    f.write_bytes(image(0x10000000, 0x10000400, family=0xE48BFF57))
    uf2.check_uf2(str(f), prof)
    f.write_bytes(image(0x10000000, 0x10000400, family=0xE48BFF56))
    with pytest.raises(SystemExit, match="is not a p2 app family"):
        uf2.check_uf2(str(f), prof)


def test_a_drive_this_user_may_write_is_copied_to_without_sudo(monkeypatch):
    """A desktop's automount (udisks) belongs to the user; a host with passwordless sudo is not
    required for it. (Whether the user may write is faked: as root, in a CI container, every
    directory is writable.)"""
    monkeypatch.setattr(uf2.os, "access", lambda path, mode: path == "/media/u/MINE")
    assert uf2.copy_argv("app.uf2", "/media/u/MINE") == ["cp", "app.uf2", "/media/u/MINE/NEW.UF2"]
    assert uf2.copy_argv("app.uf2", "/mnt/root-owned")[:3] == ["sudo", "-n", "cp"]


def _uf2_block(addr, n, total, fill=0xAB):
    blk = bytearray(512)
    struct.pack_into("<8I", blk, 0, 0x0A324655, 0x9E5D5157, 0x2000, addr, 256, n, total, 0xADA52840)
    blk[32:32 + 256] = bytes([fill]) * 256
    struct.pack_into("<I", blk, 508, 0x0AB16F30)
    return bytes(blk)


def test_a_drives_own_dump_is_cut_to_the_app_window_and_renumbered():
    """CURRENT.UF2 holds the SoftDevice too; copying back the app to leave UF2 mode must write
    nothing else (the board type's app window), as a file whose blocks count from 0."""
    addrs = [0x1000, 0x26F00, 0x27000, 0x27100, 0xD7800, 0xF4000]
    dump = b"".join(_uf2_block(a, i, len(addrs)) for i, a in enumerate(addrs))
    back = uf2.app_window(dump, 0x27000, 0xF4000)
    blocks = [struct.unpack_from("<8I", back, i) for i in range(0, len(back), 512)]
    assert [b[3] for b in blocks] == [0x27000, 0x27100, 0xD7800]
    assert [(b[5], b[6]) for b in blocks] == [(0, 3), (1, 3), (2, 3)]
    assert back[32:40] == bytes([0xAB]) * 8, "the payload is the board's own"
    assert uf2.app_window(dump, 0x100000, 0x200000) == b""


def test_a_dump_under_the_bootloaders_own_family_goes_back_under_the_app_family():
    """One XIAO bootloader labels CURRENT.UF2 0x28860045 (its USB id): the blocks written back
    carry the type's app family, so the copy's own family check passes unchanged."""
    blk = bytearray(_uf2_block(0x27000, 0, 1))
    struct.pack_into("<I", blk, 28, 0x28860045)
    back = uf2.app_window(bytes(blk), 0x27000, 0xF4000, family=0xADA52840)
    flags, fam = struct.unpack_from("<I", back, 8)[0], struct.unpack_from("<I", back, 28)[0]
    assert fam == 0xADA52840 and flags & 0x2000
    assert back[32:288] == bytes(blk[32:288]), "the payload is untouched"


def test_a_drive_the_desktop_mounts_a_moment_later_is_waited_for(monkeypatch):
    """No passwordless sudo (a desktop host): the automount that follows is used, not a failure."""
    seen = iter([None, None, None, "/media/u/XIAO-D1FB"])
    monkeypatch.setattr(uf2, "mounted_at", lambda dev: next(seen))
    monkeypatch.setattr(uf2.os, "makedirs", lambda *a, **k: None)
    no_sudo = lambda argv, **kw: type("R", (), {"returncode": 1, "stderr": "sudo: a password is required", "stdout": ""})()  # noqa: E731
    t = iter(range(100))
    mnt, ours = uf2.uf2_mount("/dev/sda", "0A0B0C0D0E0F1011", run=no_sudo, sleep=lambda _: None,
                              clock=lambda: next(t))
    assert (mnt, ours) == ("/media/u/XIAO-D1FB", False)


def test_no_sudo_and_no_automount_says_both(monkeypatch):
    monkeypatch.setattr(uf2, "mounted_at", lambda dev: None)
    monkeypatch.setattr(uf2.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(uf2.shutil, "which", lambda name: None)
    no_sudo = lambda argv, **kw: type("R", (), {"returncode": 1, "stderr": "a password is required", "stdout": ""})()  # noqa: E731
    t = iter(range(100))
    with pytest.raises(SystemExit, match="no passwordless sudo for mount, and no automount"):
        uf2.uf2_mount("/dev/sda", "0A0B0C0D0E0F1011", automount_wait=5, run=no_sudo,
                      sleep=lambda _: None, clock=lambda: next(t))
