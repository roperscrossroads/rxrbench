"""The whole-image ESP32 writer: a merged .bin linked for 0x0, compared by the loader stub's
hashes over its bootloader, partition table and app partitions, written block by block, and
only to the chip whose MAC the registry holds."""
import functools
import hashlib
import importlib.util
import json
import os

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import hwsurvey, identity, layout
from ansible_collections.rxrbench.core.plugins.module_utils.flash import chip as chipio
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, espimage
from tests.unit.esptables import row, table

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAC = "aa:bb:cc:00:11:22"
SIZE = 0x100000           # a 1 MB chip keeps the fake small; nothing here depends on the size


def helper():
    spec = importlib.util.spec_from_file_location(
        "chip_blocks", os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core",
                                    "host", "bin", "chip_blocks.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def merged(app=b"app-1", data=b"", chip="esp32", rows=None):
    """A merged image as an ESP-IDF merge-bin writes it for 0x0: 0xFF up to the bootloader (at
    0x1000 on the original ESP32), the table at 0x8000, a factory app at 0x10000, a FAT data
    partition after it, and the file ending where the last part ends."""
    rows = rows or [row(1, 0x02, 0x9000, 0x6000, "nvs"), row(0, 0x00, 0x10000, 0x40000, "factory"),
                    row(1, 0x81, 0x50000, 0x20000, "data")]
    img = bytearray(b"\xff" * 0x60000)
    boot = espimage.BOOT_OFFSET.get(chip, 0)
    img[boot:boot + 4] = b"\xe9\x03\x02\x20"
    t = table(rows)
    img[0x8000:0x8000 + len(t)] = t
    img[0x10000:0x10000 + len(app)] = app
    img[0x50000:0x50000 + len(data)] = data
    return bytes(img)


class FakeEsp:
    """The loader stub as chip_blocks uses it: flash_md5sum over any range of the real bytes,
    the chip's own MAC, a flash id whose size byte is log2 of the chip size, and writes that
    erase whole 4 KB sectors first (esptool's write_flash does)."""

    def __init__(self, flash=None, mac=MAC):
        self.flash = bytearray(flash if flash is not None else b"\xff" * SIZE)
        self.mac = mac
        self.writes = []

    def flash_md5sum(self, addr, size):
        return hashlib.md5(bytes(self.flash[addr:addr + size])).hexdigest()

    def read_flash(self, addr, size):
        return bytes(self.flash[addr:addr + size])

    def read_mac(self):
        return bytes(int(x, 16) for x in self.mac.split(":"))

    def flash_id(self):
        return (SIZE.bit_length() - 1) << 16

    def get_chip_description(self):
        return "ESP32-D0WD-V3 (revision v3.1)"


def fake_write_flash(esp, addr_data, **_kw):
    for addr, data in addr_data:
        lo, hi = addr - addr % 0x1000, -(-(addr + len(data)) // 0x1000) * 0x1000
        esp.flash[lo:hi] = b"\xff" * (hi - lo)
        esp.flash[addr:addr + len(data)] = data
        esp.writes.append((addr, len(data)))


# ---- the host tool --------------------------------------------------------------------------

def test_place_makes_the_chip_the_image_and_erased_after_it_writing_only_what_differs(tmp_path):
    cb = helper()
    img = tmp_path / "m.bin"
    img.write_bytes(merged())
    esp = FakeEsp(b"\x00" * SIZE)          # every block holds other bytes
    out = cb.place(esp, str(img), write_flash=fake_write_flash)
    assert bytes(esp.flash) == merged() + b"\xff" * (SIZE - len(merged()))
    assert out["ok"] and out["written_blocks"] == SIZE // cb.BLOCK
    again = cb.place(esp, str(img), write_flash=fake_write_flash)
    assert again["written_blocks"] == 0, "a chip that is the image is not written again"
    img.write_bytes(merged(app=b"app-2"))
    assert cb.place(esp, str(img), write_flash=fake_write_flash)["written_blocks"] == 1


def test_an_image_larger_than_the_chip_is_refused_before_anything_is_written(tmp_path):
    cb = helper()
    img = tmp_path / "big.bin"
    img.write_bytes(b"\xff" * (SIZE + 1))
    esp = FakeEsp()
    with pytest.raises(SystemExit, match="the chip only"):
        cb.place(esp, str(img), write_flash=fake_write_flash)
    assert esp.writes == []


def test_match_compares_each_region_by_the_chips_own_hash(tmp_path):
    cb = helper()
    img = tmp_path / "m.bin"
    img.write_bytes(merged())
    esp = FakeEsp(merged(app=b"app-2") + b"\xff" * (SIZE - len(merged())))
    out = cb.match(esp, str(img), [(0, 0x8000), (0x8000, 0x8C00), (0x10000, 0x50000)])
    assert [r["same"] for r in out["regions"]] == [True, True, False]
    with pytest.raises(SystemExit, match="not inside the image"):
        cb.match(esp, str(img), [(0x50000, 0x70000)])


def test_another_chip_on_the_port_is_refused_before_a_write(tmp_path, monkeypatch, capsys):
    cb = helper()
    img = tmp_path / "m.bin"
    img.write_bytes(merged())
    esp = FakeEsp(mac="aa:bb:cc:00:11:23")
    monkeypatch.setattr(cb, "connect", lambda _port: esp)
    with pytest.raises(SystemExit) as e:
        cb.main(["/dev/ttyUSB0", "place", str(img), "--mac", MAC])
    assert "not this board" in json.loads(str(e.value))["error"] and esp.writes == []
    # a read says which chip it is, so a plan can tell before it plans a write
    cb.main(["/dev/ttyUSB0", "match", str(img), "0x0:0x8000", "--mac", MAC.upper().replace(":", "")])
    assert json.loads(capsys.readouterr().out)["mac_ok"] is False


# ---- the image and the flasher ----------------------------------------------------------------

def test_the_compared_regions_are_the_bootloader_the_table_and_the_apps_not_the_data():
    assert espimage.regions(merged()) == [("boot", 0, 0x8000), ("table", 0x8000, 0x8C00),
                                          ("factory", 0x10000, 0x50000)]
    # the board's identity is the table and the apps: an update leaves the bootloader it had
    assert espimage.identity_regions(merged()) == [("table", 0x8000, 0x8C00),
                                                   ("factory", 0x10000, 0x50000)]
    # an app partition the file stops inside is compared up to where the file ends
    short = merged()[:0x30000]
    assert espimage.regions(short)[-1] == ("factory", 0x10000, 0x30000)


def test_only_a_merged_image_the_chips_rom_boots_is_written():
    assert espimage.image_refusals(merged(), "esp32") == []
    assert "no bootloader image" in espimage.image_refusals(merged(chip="esp32s3"), "esp32")[0]
    assert espimage.image_refusals(merged(chip="esp32s3"), "esp32s3") == []
    app_only = b"\xe9" + b"\xff" * 0x20000           # an application file, not a merged image
    assert any("no partition table" in r for r in espimage.image_refusals(app_only, "esp32s3"))


def staged(tmp_path, blob, family="tactile", board_tag="cyd"):
    d = tmp_path / "img"
    d.mkdir(exist_ok=True)
    (d / "m.bin").write_bytes(blob)
    manifest = {"family": family, "format": "raw-bin", "file": "m.bin", "board_tag": board_tag,
                "hash": hashlib.sha256(blob).hexdigest()}
    (d / "manifest.json").write_text(json.dumps(manifest))
    return common.load_image(str(d))


class Rig:
    """The flasher's tools faked at their seam (chip.blocks runs chip_blocks.py against a
    FakeEsp), with the port resolved and every reset counted."""

    def __init__(self, monkeypatch, esp):
        self.esp, self.resets, self.cb = esp, 0, helper()
        monkeypatch.setattr(common, "esp_port", lambda *a, **k: ("/dev/ttyUSB0", None))
        monkeypatch.setattr(chipio, "reset", self._reset)
        monkeypatch.setattr(chipio, "blocks", self._blocks)
        # the write rule reads the chip in one loader session (chip_blocks layout), never range
        # by range
        def no_single_reads(*_a, **_k):
            raise AssertionError("the write rule read a range outside its one session")
        monkeypatch.setattr(common, "read_flash", no_single_reads)
        monkeypatch.setattr(common, "tool", lambda *a, **k: (1, ""))   # busy marks: no devreg

    def _reset(self, *_a):
        self.resets += 1

    def _blocks(self, _tools, port, cmd, *args, timeout=900):  # noqa: ARG002
        argv = [port, cmd, *args]
        mac = argv[argv.index("--mac") + 1] if "--mac" in argv else None
        argv = [a for a in argv if a not in ("--mac", mac)]
        out = self.cb.info(self.esp)
        why = self.cb.mac_refusal(out, mac)
        if why and cmd == "place":
            raise common.FlashError(f"chip_blocks place failed (rc 1): {why}")
        if mac:
            out["mac_ok"] = why is None
        if cmd == "layout":
            self.layout_args = getattr(self, "layout_args", []) + [list(args)]
            args = list(args)
            at = None
            if "--at" in args:
                i = args.index("--at")
                at = [int(x, 16) for x in args[i + 1].split(",")]
                args = args[:i] + args[i + 2:]
            only = [self.cb.span(r) for r in args[0].split(",")] if args else None
            out.update(self.cb.layout(self.esp, only, at))
            self.layouts = getattr(self, "layouts", 0) + 1
        elif cmd == "match":
            out.update(self.cb.match(self.esp, argv[2],
                                     [tuple(int(x, 16) for x in r.split(":")) for r in argv[3:]]))
        elif cmd == "place":
            out.update(self.cb.place(self.esp, argv[2], write_flash=fake_write_flash))
        elif cmd in self.cb.REGION_CMDS:
            runs = [self.cb.span(r) for r in argv[argv.index("--runs") + 1].split(",")]
            argv = argv[:argv.index("--runs")]
            out.update(self.cb.region(self.esp, cmd, argv[2], self.cb.parts_of(cmd, argv[3:]),
                                      runs, write_flash=fake_write_flash))
            if not out["ok"]:
                raise common.FlashError(f"chip_blocks {cmd} failed (rc 1): {out['error']}")
        out["flash_size"] = f"{out['size'] >> 20}MB"
        return out


def flasher(tmp_path, blob, **kw):
    manifest, file = staged(tmp_path, blob)
    args = {"chip": "esp32", "board_tag": "cyd", "mac": MAC}
    args.update(kw)
    return espimage.EspImageFlasher("cyd9", manifest, file, **args), manifest


def test_a_chip_that_holds_the_image_is_not_written_even_with_its_data_changed(tmp_path, monkeypatch):
    on_chip = bytearray(merged() + b"\xff" * (SIZE - len(merged())))
    on_chip[0x9000:0x9004] = b"nvs!"           # what the firmware wrote as it ran
    on_chip[0x50000:0x50004] = b"FAT!"
    rig = Rig(monkeypatch, FakeEsp(on_chip))
    f, manifest = flasher(tmp_path, merged())
    r = common.drive(f, manifest)
    assert r["changed"] is False and r["before"]["hash"] == manifest["hash"]
    assert rig.esp.writes == [] and rig.resets == 1, "a read boots the board after it"


def test_a_new_app_under_the_same_table_is_written_and_read_back(tmp_path, monkeypatch):
    rig = Rig(monkeypatch, FakeEsp(merged(app=b"app-1") + b"\xff" * (SIZE - len(merged()))))
    f, manifest = flasher(tmp_path, merged(app=b"app-2"))
    plan = common.drive(f, manifest, check_mode=True)
    assert plan["changed"] and "factory differ" in plan["why"] and "data partitions" in plan["why"]
    assert rig.esp.writes == []
    r = common.drive(f, manifest)
    assert r["changed"] and identity.same(r["after"], r["image"]) is True
    assert bytes(rig.esp.flash[:len(merged())]) == merged(app=b"app-2")


def test_other_firmware_is_a_first_flash_with_the_surveys_checks(tmp_path, monkeypatch):
    other = merged(rows=[row(1, 0x02, 0x9000, 0x5000, "nvs"), row(0, 0x20, 0x10000, 0x150000, "test")])
    Rig(monkeypatch, FakeEsp(other + b"\xff" * (SIZE - len(other))))
    f, manifest = flasher(tmp_path, merged())
    with pytest.raises(common.FlashError, match="first flash; declare bench_first_flash"):
        common.drive(f, manifest, check_mode=True)
    f, manifest = flasher(tmp_path, merged(), first_flash=True)
    with pytest.raises(common.FlashError, match="survey facts do not show"):
        common.drive(f, manifest, check_mode=True)
    f, manifest = flasher(tmp_path, merged(), first_flash=True,
                          facts_tags=["secure-boot:off", "flash-encryption:off", "rom-download:on"])
    plan = common.drive(f, manifest, check_mode=True)
    assert plan["changed"] and "a first flash" in plan["why"]


def test_an_app_manager_with_apps_is_not_written_over_without_the_layouts_word(tmp_path, monkeypatch):
    Rig(monkeypatch, FakeEsp())
    tags = ["secure-boot:off", "flash-encryption:off", "rom-download:on", "app-manager:test",
            "app-manager-apps:2"]
    f, manifest = flasher(tmp_path, merged(), first_flash=True, facts_tags=tags)
    with pytest.raises(common.FlashError, match="bench_app_manager_overwrite"):
        common.drive(f, manifest, check_mode=True)
    f, manifest = flasher(tmp_path, merged(), first_flash=True, facts_tags=tags,
                          manager_overwrite=True)
    assert common.drive(f, manifest, check_mode=True)["changed"]


def test_at_the_write_the_chip_itself_is_compared_and_another_firmwares_images_are_kept(tmp_path, monkeypatch):
    """The plan has the survey's word; the apply asks the chip (docs/ARCHITECTURE.md#partitions-and-the-write-rule): a
    manager with an installed app holding an image is foreign to a first flash of the whole chip."""
    from tests.unit import esptables
    rows = list(esptables.MANAGED_4MB) + [row(0, 0x10, 0x160000, 0x100000, "game")]
    on_chip = esptables.chip(rows, images=[0x10000, 0x160000], boot_seq=None)
    rig = Rig(monkeypatch, FakeEsp(on_chip + b"\xff" * (SIZE - len(on_chip))))
    safe = ["secure-boot:off", "flash-encryption:off", "rom-download:on"]
    f, manifest = flasher(tmp_path, merged(), first_flash=True, facts_tags=safe)
    with pytest.raises(common.FlashError, match="the new partition table drops game at 0x160000, which holds an image"):
        common.drive(f, manifest)
    assert rig.esp.writes == [] and rig.layouts == 1, "one loader session for the rule's reads"
    f, manifest = flasher(tmp_path, merged(), first_flash=True, facts_tags=safe, manager_overwrite=True)
    assert common.drive(f, manifest)["changed"]


def test_the_plan_runs_the_write_rule_over_the_surveys_record_and_honours_bench_overwrite(tmp_path, monkeypatch):
    """A plan reads nothing from the chip, so it asks the rule of the survey's record; it gives
    the chip's verdict and takes the per-region go-ahead (bench_overwrite) as the apply does,
    not only bench_app_manager_overwrite (docs/ARCHITECTURE.md#partitions-and-the-write-rule)."""
    from tests.unit import esptables
    rows = list(esptables.MANAGED_4MB) + [row(0, 0x10, 0x160000, 0x100000, "game")]
    on_chip = esptables.chip(rows, images=[0x10000, 0x160000], boot_seq=None)
    rig = Rig(monkeypatch, FakeEsp(on_chip + b"\xff" * (SIZE - len(on_chip))))
    record = esptables.recorded(rows, images=[0x10000, 0x160000], boot_seq=None)
    safe = ["secure-boot:off", "flash-encryption:off", "rom-download:on", "app-manager:test",
            "app-manager-apps:1"]
    f, manifest = flasher(tmp_path, merged(), first_flash=True, facts_tags=safe, facts_chip=record)
    with pytest.raises(common.FlashError) as e:
        common.drive(f, manifest, check_mode=True)
    assert "the new partition table drops game at 0x160000" in str(e.value)
    assert "(as surveyed 2026-11-07T18:00:00Z)" in str(e.value)
    assert rig.esp.writes == [] and getattr(rig, "layouts", 0) == 0, "a plan asks the record, not the chip"
    # naming some regions leaves the rest refused
    f, manifest = flasher(tmp_path, merged(), first_flash=True, facts_tags=safe, facts_chip=record,
                          overwrite=["game"])
    with pytest.raises(common.FlashError) as e:
        common.drive(f, manifest, check_mode=True)
    assert "game at 0x160000" not in str(e.value) and "nvs at 0x9000" in str(e.value)
    # naming every region the write takes is the go-ahead, at the plan and at the apply
    every = ["nvs", "otadata", "game"]   # test, the app the chip boots, is the layout's already
    f, manifest = flasher(tmp_path, merged(), first_flash=True, facts_tags=safe, facts_chip=record,
                          overwrite=every)
    assert common.drive(f, manifest, check_mode=True)["changed"]
    assert common.drive(f, manifest)["changed"]


def test_a_spot_check_before_a_write_reads_the_overlapped_slots_alone(tmp_path, monkeypatch):
    """Checking depth before a write (docs/ARCHITECTURE.md#checking-depth): spot reads the table and the heads of the slots the write
    overlaps, the only ones it can destroy; the verdict is the full read's."""
    from tests.unit import esptables
    rows = list(esptables.MANAGED_4MB) + [row(0, 0x10, 0x160000, 0x100000, "camstr"),
                                          row(0, 0x11, 0x260000, 0xE0000, "wifite")]
    on_chip = esptables.chip(rows, images=[0x10000, 0x160000, 0x260000], boot_seq=1)
    rig = Rig(monkeypatch, FakeEsp(on_chip + b"\xff" * (SIZE - len(on_chip))))
    blocks = functools.partial(chipio.blocks, {})
    into = [(0x160000, 0x1000)]
    assert common.esp_write_refusals("esptool", "esp32", "/dev/x", into, check_depth="spot", blocks=blocks) == []
    assert rig.layout_args[-1] == ["0x160000:0x161000"]
    over = [(0x10000, 0x1000)]
    spot = common.esp_write_refusals("esptool", "esp32", "/dev/x", over, check_depth="spot", blocks=blocks)
    full = common.esp_write_refusals("esptool", "esp32", "/dev/x", over, check_depth="full", blocks=blocks)
    assert spot == full and len(spot) == 1 and "covers test at 0x10000" in spot[0]
    assert rig.layout_args[-2:] == [["0x10000:0x11000"], []]


def test_a_spot_check_whose_new_table_drops_a_region_reads_again_in_full(tmp_path, monkeypatch):
    from tests.unit import esptables
    rows = list(esptables.MANAGED_4MB) + [row(0, 0x10, 0x160000, 0x100000, "game")]
    on_chip = esptables.chip(rows, images=[0x10000, 0x160000], boot_seq=None)
    rig = Rig(monkeypatch, FakeEsp(on_chip + b"\xff" * (SIZE - len(on_chip))))
    new = esptables.table([row(1, 0x02, 0x9000, 0x5000, "nvs"), row(0, 0x10, 0x10000, 0x100000, "app")])
    why = common.esp_write_refusals("esptool", "esp32", "/dev/x", [(0x10000, 0x1000)], check_depth="spot",
                                    new_table=new, blocks=functools.partial(chipio.blocks, {}))
    assert rig.layout_args == [["0x10000:0x11000"], []], "a table change reads in full"
    assert any("drops game at 0x160000" in w for w in why)


def test_spot_is_taken_only_beside_a_full_record_and_never_for_a_table_change(tmp_path, monkeypatch):
    from tests.unit import esptables
    Rig(monkeypatch, FakeEsp(merged(app=b"app-1") + b"\xff" * (SIZE - len(merged()))))
    plain = [row(1, 0x02, 0x9000, 0x6000, "nvs"), row(0, 0x00, 0x10000, 0x40000, "factory"),
             row(1, 0x81, 0x50000, 0x20000, "data")]          # merged()'s own table
    record = esptables.recorded(plain, images=[0x10000])
    f, manifest = flasher(tmp_path, merged(app=b"app-2"), facts_chip=record)
    assert common.drive(f, manifest, check_mode=True, check_depth="spot")["checked"] == \
        {"depth": "spot", "reason": "asked"}
    f, manifest = flasher(tmp_path, merged(app=b"app-2"))
    assert common.drive(f, manifest, check_mode=True, check_depth="spot")["checked"] == \
        {"depth": "full", "reason": "no full record yet for the firmware the board reports"}
    other = merged(rows=[row(1, 0x02, 0x9000, 0x5000, "nvs"), row(0, 0x20, 0x10000, 0x150000, "test")])
    Rig(monkeypatch, FakeEsp(other + b"\xff" * (SIZE - len(other))))
    f, manifest = flasher(tmp_path, merged(), facts_chip=record, first_flash=True,
                          facts_tags=["secure-boot:off", "flash-encryption:off", "rom-download:on"])
    assert common.drive(f, manifest, check_mode=True, check_depth="spot")["checked"]["depth"] == "full"


def test_an_mcuboot_layouts_regions_come_from_the_images_map():
    fj = {"map": {"BOOT": "0x0", "BOOT_SIZE": "0x10000", "SLOT0": "0x20000", "SLOT0_SIZE": "0x2d0000",
                  "SLOT1": "0x2f0000", "SLOT1_SIZE": "0x2d0000", "STORAGE": "0x7b0000",
                  "STORAGE_SIZE": "0x30000", "FLASH_SIZE": "0x800000"}}
    assert [(r["label"], r["kind"], r["start"], r["size"]) for r in common.flash_map_regions(fj)] == [
        ("boot", "boot", 0, 0x10000), ("slot0", "app", 0x20000, 0x2d0000),
        ("slot1", "app", 0x2f0000, 0x2d0000), ("storage", "data", 0x7b0000, 0x30000)]
    old = {"partitions": {"slot0": "0x20000", "slot1": "0x220000", "storage": "0x420000",
                          "storage_size": "0x30000"}}
    assert [(r["label"], r["start"], r["size"]) for r in common.flash_map_regions(old)] == [
        ("slot0", 0x20000, 0x200000), ("slot1", 0x220000, 0x200000), ("storage", 0x420000, 0x30000)]
    assert common.flash_map_regions({}) == []


MCUBOOT_HEADER = bytes.fromhex("3db8f396") + b"\0" * 60


def test_on_a_chip_with_no_table_an_mcuboot_image_is_the_layouts_and_any_other_is_foreign(monkeypatch):
    """A chip with no partition table (docs/ARCHITECTURE.md#partitions-and-the-write-rule): the image's map gives the regions; an
    MCUboot image in a slot is the layout's, an ESP-IDF app where the map puts a slot is
    foreign, an erased start loses nothing."""
    fmap = common.flash_map_regions({"partitions": {"slot0": "0x20000", "slot1": "0x120000",
                                                    "storage": "0x220000", "storage_size": "0x10000"}})
    into = [(0x20000, 0x1000)]

    def chip_with(head):
        flash = bytearray(b"\xff" * 0x230000)
        flash[0x20000:0x20000 + len(head)] = head
        rig = Rig(monkeypatch, FakeEsp(bytes(flash)))
        return rig, functools.partial(chipio.blocks, {})

    rig, blocks = chip_with(MCUBOOT_HEADER)
    assert common.esp_write_refusals("e", "esp32s3", "/dev/x", into, blocks=blocks, flash_map=fmap) == []
    assert rig.layout_args[-1] == ["--at", "0x20000,0x120000"], "the map's slots, in the write's session"
    _rig, blocks = chip_with(espimage_app_header())
    why = common.esp_write_refusals("e", "esp32s3", "/dev/x", into, blocks=blocks, flash_map=fmap)
    assert len(why) == 1 and "covers slot0 at 0x20000" in why[0] and "esp-app-image" in why[0]
    assert common.esp_write_refusals("e", "esp32s3", "/dev/x", into, blocks=blocks, flash_map=fmap,
                                     allowed=("slot0",)) == []
    _rig, blocks = chip_with(b"")
    assert common.esp_write_refusals("e", "esp32s3", "/dev/x", into, blocks=blocks, flash_map=fmap) == []
    rig, blocks = chip_with(espimage_app_header())
    common.esp_write_refusals("e", "esp32s3", "/dev/x", into, blocks=blocks, flash_map=fmap, check_depth="spot")
    assert rig.layout_args[-1] == ["0x20000:0x21000", "--at", "0x20000"], "spot: the overlapped slot"


def espimage_app_header():
    from tests.unit import esptables
    return esptables.APP_HEADER


def test_the_recorded_rule_on_its_own():
    """common.recorded_write_refusals: the declared boot app is the layout's, other images and
    shared data are named, `all` takes everything, and a record from before the survey kept an
    inventory falls back to the manager tag (survey again)."""
    from tests.unit import esptables
    rows = list(esptables.MANAGED_4MB) + [row(0, 0x10, 0x160000, 0x100000, "camstr"),
                                          row(0, 0x11, 0x260000, 0xE0000, "wifite")]
    record = esptables.recorded(rows, images=[0x10000, 0x160000, 0x260000], boot_seq=1)
    assert common.recorded_write_refusals(record, [], [(0x160000, 0x1000)]) == [], "its boot app"
    assert common.recorded_write_refusals(record, [], [(0x8000, 0xC00)]) == [], \
        "the table's own region is the chip's, as the chip's rule knows it (an update rewrites it)"
    why = common.recorded_write_refusals(record, [], [(0x260000, 0x1000)], what="writing the slot")
    assert why == ["writing the slot: the write covers wifite at 0x260000, which holds an image the "
                   "layout does not declare (esp-app-image); name it in bench_overwrite if that is "
                   "meant (as surveyed 2026-11-07T18:00:00Z)"]
    assert common.recorded_write_refusals(record, [], [(0x260000, 0x1000)], ["wifite"]) == []
    whole = common.recorded_write_refusals(record, [], common.WHOLE_CHIP)
    assert [w.split(" at ")[0].rsplit(" ", 1)[1] for w in whole] == ["nvs", "otadata", "test", "wifite"]
    assert common.recorded_write_refusals(record, [], common.WHOLE_CHIP, ("all",)) == []
    tags = ["app-manager:test", "app-manager-apps:2"]
    old = common.recorded_write_refusals({}, tags, common.WHOLE_CHIP)
    assert len(old) == 1 and "kept no record of its regions" in old[0]
    assert common.recorded_write_refusals({}, tags, common.WHOLE_CHIP, ("all",)) == []
    assert common.recorded_write_refusals({}, [], common.WHOLE_CHIP) == [], "never surveyed deep"


def test_nothing_is_written_to_a_chip_the_registry_does_not_name(tmp_path, monkeypatch):
    rig = Rig(monkeypatch, FakeEsp(mac="aa:bb:cc:00:11:23"))
    f, manifest = flasher(tmp_path, merged())
    with pytest.raises(common.FlashError, match="another board is plugged in"):
        common.drive(f, manifest, check_mode=True)
    f, manifest = flasher(tmp_path, merged(), mac=None)
    rig.esp.mac = MAC
    with pytest.raises(common.FlashError, match="registry holds no MAC"):
        common.drive(f, manifest, check_mode=True)
    assert rig.esp.writes == []


def test_a_manifest_that_does_not_describe_its_file_is_refused(tmp_path, monkeypatch):
    Rig(monkeypatch, FakeEsp())
    f, manifest = flasher(tmp_path, merged())
    with open(f.image, "r+b") as fh:
        fh.seek(0x10000)
        fh.write(b"tampered")
    with pytest.raises(common.FlashError, match="SHA-256 differs"):
        common.drive(f, manifest, check_mode=True)


# ---- what the registry and the survey give it ------------------------------------------------

def test_the_chips_mac_comes_from_the_policy_or_a_usb_serial_that_is_one():
    assert layout.chip_mac({"policy": {"chip_mac": "AA:BB:CC:00:11:22"}}) == MAC
    assert layout.chip_mac({"match": {"usb_serial": "AABBCC001122"}}) == MAC
    assert layout.chip_mac({"match": {"usb_vid": "1a86"},
                            "match_also": [{"usb_serial": "aa:bb:cc:00:11:22"}]}) == MAC
    assert layout.chip_mac({"match": {"usb_serial": "0123456789ABCDEF"}}) is None, "a chip id is not a MAC"
    assert layout.chip_mac({"match": {"usb_vid": "1a86", "usb_pid": "7523"}}) is None


def test_the_original_esp32_names_its_security_efuses_its_own_way():
    """espefuse's names on an ESP32-D0WD-V3, as a deep survey of one recorded them."""
    def tags(efuse_all):
        return set(hwsurvey.tags({"class": {}, "probes": {"esptool": {
            "chip": "ESP32-D0WD-V3", "efuse": {"MAC": MAC}, "efuse_all": efuse_all}}}))
    safe = {"ABS_DONE_0": False, "ABS_DONE_1": False, "FLASH_CRYPT_CNT": 0,
            "UART_DOWNLOAD_DIS": False}
    assert {"secure-boot:off", "flash-encryption:off", "rom-download:on"} <= tags(safe)
    assert "secure-boot:on" in tags({**safe, "ABS_DONE_1": True})
    assert "flash-encryption:on" in tags({**safe, "FLASH_CRYPT_CNT": 1})
    assert "flash-encryption:off" in tags({**safe, "FLASH_CRYPT_CNT": 3}), "an even count is off"
    assert "rom-download:off" in tags({**safe, "UART_DOWNLOAD_DIS": True})


def test_a_board_with_no_broker_is_still_backed_up_and_restored_through_its_loader(tmp_path):
    """A firmware with no console has no broker (hosts-setup retires it); the chip modules
    then hold nothing, rather than refusing for want of a socket."""
    from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
    b = broker_client.board_or_none("cyd9", path=str(tmp_path / "cyd9.sock"))
    assert isinstance(b, broker_client.NoBroker)
    with b:
        assert b.release(ttl=10) == {"ok": True} and b.reacquire() == {"ok": True}
    (tmp_path / "cyd9.sock").write_text("")
    assert isinstance(broker_client.board_or_none("cyd9", path=str(tmp_path / "cyd9.sock")),
                      broker_client.Board)


def test_diff_counts_what_a_write_would_write_and_writes_nothing(tmp_path):
    """A restore's or a switch's plan (not "? of ? blocks"): the chip hashed
    against the image by the stub, the same count `write` then writes."""
    cb = helper()
    img = tmp_path / "chip.bin"
    img.write_bytes(merged(app=b"app-2") + b"\xff" * (SIZE - len(merged())))
    esp = FakeEsp(merged() + b"\xff" * (SIZE - len(merged())))
    d = cb.diff(esp, str(img))
    assert d["ok"] and d["blocks"] == SIZE // cb.BLOCK and d["differing_blocks"] == 1
    assert esp.writes == []
    assert cb.write(esp, str(img), write_flash=fake_write_flash)["written_blocks"] == 1
    assert cb.diff(esp, str(img))["differing_blocks"] == 0


BOOT_RADIO = ["cyd9: its channel 869.5870-869.6490 MHz is outside the band 902.0-928.0 MHz"]


def test_a_fresh_write_whose_image_boots_out_of_band_is_refused_without_the_layouts_word(
        tmp_path, monkeypatch):
    """A first flash of a stock mesh release must not boot on its build's radio (outside the site's
    band) and advertise before its config is written."""
    other = merged(rows=[row(1, 0x02, 0x9000, 0x5000, "nvs"), row(0, 0x20, 0x10000, 0x150000, "test")])
    Rig(monkeypatch, FakeEsp(other + b"\xff" * (SIZE - len(other))))
    tags = ["secure-boot:off", "flash-encryption:off", "rom-download:on"]
    f, manifest = flasher(tmp_path, merged(), first_flash=True, facts_tags=tags)
    with pytest.raises(common.FlashError, match="dummy load"):
        common.drive(f, manifest, check_mode=True, boot_radio=BOOT_RADIO)
    plan = common.drive(f, manifest, check_mode=True, boot_radio=BOOT_RADIO, boot_radio_ok=True)
    assert plan["changed"]


def test_an_update_under_the_same_table_keeps_its_settings_and_is_not_held_to_the_boot_radio(
        tmp_path, monkeypatch):
    Rig(monkeypatch, FakeEsp(merged(app=b"app-1") + b"\xff" * (SIZE - len(merged()))))
    f, manifest = flasher(tmp_path, merged(app=b"app-2"))
    assert common.drive(f, manifest, check_mode=True, boot_radio=BOOT_RADIO)["changed"]


# ---- update and full install, as a vendor's web installer does them --------------------------

def app(tag, segments=2):
    """An ESP-IDF app image: 24-byte header (magic, segment count, ..., hash appended), each
    segment an 8-byte header and its bytes, the checksum ending a 16-byte line, a SHA-256."""
    body = b""
    for i in range(segments):
        data = (tag + bytes([i])) * 7
        body += (0x3C000000 + i).to_bytes(4, "little") + len(data).to_bytes(4, "little") + data
    head = bytes([0xE9, segments, 2, 0x20]) + b"\0" * 19 + b"\x01"
    img = head + body
    img += b"\0" * (15 - len(img) % 16) + b"\x5a"
    return img + hashlib.sha256(img).digest()


OTA_ROWS = [row(1, 0x02, 0x9000, 0x5000, "nvs"), row(1, 0x00, 0xE000, 0x2000, "otadata"),
            row(0, 0x10, 0x10000, 0x30000, "app0"), row(0, 0x11, 0x40000, 0x30000, "app1"),
            row(1, 0x82, 0x70000, 0x40000, "spiffs")]


def release(tag=b"v2", boot=b"\xe9\x03\x02\x20", loader=b"ota-1"):
    """What a release's full install leaves on a chip: bootloader, table, the app in app0, an
    OTA loader app in app1 and a file system image in spiffs (the staged chip.bin)."""
    img = bytearray(b"\xff" * 0x71000)
    img[0:len(boot)] = boot
    t = table(OTA_ROWS)
    img[0x8000:0x8000 + len(t)] = t
    a = app(tag)
    img[0x10000:0x10000 + len(a)] = a
    o = app(loader)
    img[0x40000:0x40000 + len(o)] = o
    img[0x70000:0x70010] = b"littlefs-head..."
    return bytes(img)


def on_chip(blob, prefs=b"prefs+key"):
    """The chip of a node that has run `blob`: its file system and NVS written as it ran."""
    chip = bytearray(blob + b"\xff" * (SIZE - len(blob)))
    chip[0x70000:0x70000 + len(prefs)] = prefs
    chip[0x9000:0x9004] = b"nvs!"
    return chip


def test_an_app_image_is_compared_as_long_as_it_is_not_its_whole_partition():
    a = app(b"v2")
    assert espimage.app_length(release(), 0x10000, 0x40000) == len(a)
    assert espimage.identity_regions(release()) == [
        ("table", 0x8000, 0x8C00), ("app0", 0x10000, 0x10000 + len(a)),
        ("app1", 0x40000, 0x40000 + len(app(b"ota-1")))]
    assert espimage.app_length(b"\xe9\x05" + b"\0" * 30, 0, 32) is None, "segments past the end"


def test_an_update_writes_the_apps_only_and_keeps_bootloader_settings_and_key(tmp_path, monkeypatch):
    """An apply of a newer stock release keeps the node's key: like the vendor's installer without
    its full-erase box, it writes the apps and nothing else."""
    old = release(tag=b"v1", boot=b"\xe9\x03\x02\x10", loader=b"ota-0")
    rig = Rig(monkeypatch, FakeEsp(on_chip(old)))
    f, manifest = flasher(tmp_path, release(), chip="esp32s3")
    plan = common.drive(f, manifest, check_mode=True)
    assert plan["changed"] and "an update" in plan["why"] and "app0, app1" in plan["why"]
    assert f.fresh is False, "an update keeps the config, so the boot radio is not asked"
    r = common.drive(f, manifest)
    assert r["changed"] and identity.same(r["after"], r["image"]) is True
    chip = bytes(rig.esp.flash)
    assert chip[0x70000:0x70009] == b"prefs+key" and chip[0x9000:0x9004] == b"nvs!"
    assert chip[0:4] == b"\xe9\x03\x02\x10", "the bootloader the board had is kept"
    assert f.note and "bootloader" in f.note
    assert {a for a, _n in rig.esp.writes} == {0x10000, 0x40000}


def test_a_board_whose_bootloader_alone_differs_runs_the_image(tmp_path, monkeypatch):
    rig = Rig(monkeypatch, FakeEsp(on_chip(release(boot=b"\xe9\x03\x02\x10"))))
    f, manifest = flasher(tmp_path, release(), chip="esp32s3")
    r = common.drive(f, manifest)
    assert r["changed"] is False and rig.esp.writes == [] and "bootloader" in f.note


def test_a_full_erase_is_the_whole_chip_even_on_a_board_that_runs_the_image(tmp_path, monkeypatch):
    rig = Rig(monkeypatch, FakeEsp(on_chip(release(boot=b"\xe9\x03\x02\x10"))))
    f, manifest = flasher(tmp_path, release(), chip="esp32s3", full_erase=True)
    plan = common.drive(f, manifest, check_mode=True, boot_radio=BOOT_RADIO, boot_radio_ok=True)
    assert plan["changed"] and "full install" in plan["why"] and f.fresh
    with pytest.raises(common.FlashError, match="dummy load"):
        common.drive(f, manifest, check_mode=True, boot_radio=BOOT_RADIO)
    r = common.drive(f, manifest)
    assert r["changed"] and identity.same(r["after"], r["image"]) is True
    assert bytes(rig.esp.flash) == release() + b"\xff" * (SIZE - len(release()))


def test_staging_lays_a_releases_partition_images_into_its_merged_file(tmp_path):
    spec = importlib.util.spec_from_file_location("stage_raw", os.path.join(REPO, "tools", "stage_raw.py"))
    sr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sr)
    full = release()
    factory = full[:0x10000 + len(app(b"v2"))]
    (tmp_path / "f.bin").write_bytes(factory)
    (tmp_path / "ota.bin").write_bytes(app(b"ota-1"))
    (tmp_path / "fs.bin").write_bytes(b"littlefs-head..." + b"\xff" * 0x100)
    blob, placed = sr.compose(str(tmp_path / "f.bin"), [("ota_1", str(tmp_path / "ota.bin")),
                                                         ("spiffs", str(tmp_path / "fs.bin"))])
    assert blob == full.rstrip(b"\xff")
    assert [(p["label"], p["offset"]) for p in placed] == [("app1", "0x40000"), ("spiffs", "0x70000")]
    with pytest.raises(SystemExit, match="no partition 'fat'"):
        sr.compose(str(tmp_path / "f.bin"), [("fat", str(tmp_path / "fs.bin"))])
    with pytest.raises(SystemExit, match="already fills"):
        sr.compose(str(tmp_path / "f.bin"), [("app0", str(tmp_path / "ota.bin"))])


def test_a_board_devreg_says_is_not_there_is_unanswered_until_something_is_written(
        tmp_path, monkeypatch):
    """Found on hardware: a board the plug watcher cannot see (a bridge with no serial) is
    assumed present and its first read decides; devreg said it was not there, and the run failed
    instead of skipping it. After a write the same answer stays a failure: the write is not
    checked."""
    Rig(monkeypatch, FakeEsp(bytearray(merged() + b"\xff" * (SIZE - len(merged())))))
    absent = common.Absent("devreg cannot resolve cyd9: 'cyd9' is registered but NOT PRESENT")
    monkeypatch.setattr(common, "esp_port", lambda *a, **k: (None, absent))
    f, manifest = flasher(tmp_path, merged(b"app-2"))
    with pytest.raises(common.Unanswered, match="NOT PRESENT"):
        common.drive(f, manifest)
    f.wrote = {"app0": "written"}
    with pytest.raises(common.FlashError) as e:
        f._port()
    assert not isinstance(e.value, common.Unanswered)
    monkeypatch.setattr(common, "esp_port", lambda *a, **k: (None, "devreg guard refuses it"))
    f.wrote = None
    with pytest.raises(common.FlashError) as e:
        f._port()
    assert not isinstance(e.value, common.Unanswered), "only devreg's 'not here' is unanswered"


# ---- which slot boots (otadata) --------------------------------------------------------------

def otadata(seq):
    """An otadata partition (0x2000 bytes) whose first entry holds `seq`, as ESP-IDF writes it:
    seq selects ota_((seq - 1) % slots); 0xFFFFFFFF state is a valid entry (ESP_OTA_IMG_UNDEFINED)."""
    import binascii
    import struct
    entry = struct.pack("<I", seq) + b"\xff" * 20 + struct.pack("<I", 0xFFFFFFFF)
    entry += struct.pack("<I", binascii.crc32(struct.pack("<I", seq), 0xFFFFFFFF) & 0xFFFFFFFF)
    return entry + b"\xff" * (0x2000 - len(entry))


def booting(blob, seq):
    """`blob` with its otadata selecting slot (seq - 1) % 2."""
    out = bytearray(blob)
    out[0xE000:0x10000] = otadata(seq)
    return bytes(out)


def test_match_says_which_slot_the_chip_boots(tmp_path):
    cb = helper()
    img = tmp_path / "i.bin"
    img.write_bytes(release())
    out = cb.match(FakeEsp(on_chip(booting(release(), 2))), str(img), [(0x8000, 0x8C00)])
    assert bytes.fromhex(out["otadata"]) == otadata(2)
    out = cb.match(FakeEsp(), str(img), [(0x8000, 0x8C00)])
    assert out["otadata"] is None, "no table on the chip: nothing to say"


def test_a_board_that_boots_another_slot_does_not_run_the_image_and_an_update_points_it_back(
        tmp_path, monkeypatch):
    """A Meshtastic board left in its OTA loader (app1) holds the release's apps byte for byte,
    yet runs the loader: the update writes the image's otadata, and nothing else."""
    image = booting(release(), 1)
    rig = Rig(monkeypatch, FakeEsp(on_chip(booting(release(), 2))))
    f, manifest = flasher(tmp_path, image, chip="esp32s3")
    plan = common.drive(f, manifest, check_mode=True)
    assert plan["changed"] and "boots app1, not app0" in plan["why"]
    assert "boots app1" in plan["before"]["hash"] and rig.esp.writes == []
    r = common.drive(f, manifest)
    assert r["changed"] and identity.same(r["after"], r["image"]) is True
    assert rig.esp.writes == [(0xE000, 0x2000)], "the otadata partition alone"
    chip = bytes(rig.esp.flash)
    assert chip[0xE000:0x10000] == otadata(1) and chip[0x70000:0x70009] == b"prefs+key"


def test_a_board_updated_over_the_air_that_boots_the_images_slot_runs_it(tmp_path, monkeypatch):
    """otadata is compared by the slot it selects, not byte for byte: an OTA raises the sequence."""
    rig = Rig(monkeypatch, FakeEsp(on_chip(booting(release(), 3))))
    f, manifest = flasher(tmp_path, booting(release(), 1), chip="esp32s3")
    r = common.drive(f, manifest)
    assert r["changed"] is False and rig.esp.writes == []


def test_an_update_writes_only_the_apps_that_differ(tmp_path, monkeypatch):
    rig = Rig(monkeypatch, FakeEsp(on_chip(release(tag=b"v1"))))
    f, manifest = flasher(tmp_path, release(), chip="esp32s3")
    r = common.drive(f, manifest)
    assert r["changed"] and {a for a, _n in rig.esp.writes} == {0x10000}


def test_a_bench_host_copy_that_does_not_say_which_slot_boots_is_named(tmp_path, monkeypatch):
    rig = Rig(monkeypatch, FakeEsp(on_chip(release())))
    plain = rig._blocks

    def old_copy(*a, **k):
        out = plain(*a, **k)
        out.pop("otadata", None)
        return out
    monkeypatch.setattr(chipio, "blocks", old_copy)
    f, manifest = flasher(tmp_path, release(), chip="esp32s3")
    with pytest.raises(common.FlashError, match="hosts-setup"):
        common.drive(f, manifest, check_mode=True)


def test_the_chips_mac_comes_from_a_serial_by_id_that_carries_one():
    """An ESP32's USB-Serial-JTAG names its port by the MAC: the registry's identity, which
    every esptool write and a whole-chip erase then check the chip against."""
    jtag = "usb-Espressif_USB_JTAG_serial_debug_unit_AA:BB:CC:00:11:22-if00"
    assert layout.chip_mac({"match": {"serial_by_id": jtag}}) == MAC
    assert layout.chip_mac({"match": {"serial_by_id": "usb-Espressif_x_AABBCC001122-if00"}}) == MAC
    assert layout.chip_mac({"policy": {"chip_mac": "aa:bb:cc:00:11:23"},
                            "match": {"serial_by_id": jtag}}) == "aa:bb:cc:00:11:23", "the policy first"
    assert layout.chip_mac({"match": {"serial_by_id": "usb-1a86_USB_Serial-if00-port0"}}) is None
