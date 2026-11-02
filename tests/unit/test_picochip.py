"""Whole-chip backup, restore and erase of an RP2040/RP2350 through its ROM bootloader
(flash/picochip.py), against the fake picotool of test_picoboot (a flash file and a state)."""
import gzip
import hashlib
import json
import os

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import access, common, picochip
import sys  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
from test_picoboot import CHIP, chip  # noqa: E402,F401  (the fixture)

SPEC = {"family": 0xE48BFF56, "app": [0x10000000, 0x10040000], "enter": ["reset-interface"]}


class Done(Exception):
    pass


class Module:
    def __init__(self, check_mode=False):
        self.check_mode, self.result = check_mode, None

    def exit_json(self, **kw):
        self.result = kw
        raise Done()


def params(tmp, tool, **kw):
    return {"board": "pog1", "chip_id": CHIP, "uf2": SPEC, "dest": str(tmp / "backups"),
            "tools": {"picotool": tool, "devreg": str(tmp / "no-devreg")}, "chip": "rp2040",
            "socket": str(tmp / "no.sock"), "enter_wait": 1, "apply_allowed": True, **kw}


def run(fn, *a, check_mode=False):
    m = Module(check_mode)
    with pytest.raises(Done):
        fn(m, *a)
    return m.result


def test_a_backup_records_the_chip_and_its_whole_flash(chip):
    tmp, tool, _, _, flash = chip
    flash.write_bytes(os.urandom(0x40000))
    r = run(picochip.backup, params(tmp, tool))
    meta = json.loads(open(os.path.join(r["path"], "meta.json")).read())
    assert meta["chip"]["chip_id"] == CHIP and meta["size"] == 0x40000 and meta["route"] == "picoboot"
    with gzip.open(os.path.join(r["path"], "flash.bin.gz")) as g:
        assert g.read() == flash.read_bytes()
    assert meta["sha256"] == hashlib.sha256(flash.read_bytes()).hexdigest()
    assert json.loads((tmp / "state.json").read_text())["boot"] is False     # back in its app


def test_a_restore_writes_only_what_differs_and_must_hash_to_the_backup(chip):
    tmp, tool, _, log, flash = chip
    original = os.urandom(0x40000)
    flash.write_bytes(original)
    path = run(picochip.backup, params(tmp, tool))["path"]
    changed = bytearray(original)
    changed[0x5000:0x5010] = b"\0" * 16                  # one sector
    changed[0x3F000] ^= 0xFF                             # another
    flash.write_bytes(bytes(changed))
    plan = run(picochip.restore, params(tmp, tool), path, check_mode=True)
    assert plan["sectors"] == 2 and flash.read_bytes() == bytes(changed)  # a plan writes nothing
    done = run(picochip.restore, params(tmp, tool), path)
    assert done["changed"] and done["written_blocks"] == 2 and flash.read_bytes() == original
    assert run(picochip.restore, params(tmp, tool), path)["changed"] is False


def test_another_chips_backup_is_refused(chip):
    tmp, tool, _, _, _ = chip
    path = run(picochip.backup, params(tmp, tool))["path"]
    with pytest.raises(common.FlashError, match="belongs to the chip it was read from"):
        picochip.restore(Module(), params(tmp, tool, chip_id="0123456789ABCDEF"), path)


def test_a_whole_chip_erase_needs_a_backup_of_this_chip_and_the_plan_says_which(chip):
    tmp, tool, _, _, flash = chip
    data = os.urandom(0x40000)
    flash.write_bytes(data)
    plan = run(picochip.erase, params(tmp, tool), check_mode=True)
    assert plan["changed"] and "no backup of this chip" in plan["msg"] and "just backup" in plan["msg"]
    with pytest.raises(common.FlashError, match="has no backup of this chip.*erase_without_backup"):
        picochip.erase(Module(), params(tmp, tool))
    assert flash.read_bytes() == data, "refused: nothing erased"
    path = run(picochip.backup, params(tmp, tool))["path"]
    plan = run(picochip.erase, params(tmp, tool), check_mode=True)
    assert plan["backups"] == [os.path.basename(path)] and os.path.basename(path) in plan["msg"]
    assert plan["chip"]["chip_id"] == CHIP and plan["size"] == 0x40000
    assert flash.read_bytes() == data, "a plan erases nothing"
    done = run(picochip.erase, params(tmp, tool))
    assert done["changed"] and "read back erased" in done["msg"]
    assert flash.read_bytes() == b"\xff" * 0x40000


def test_another_chips_backup_does_not_count_for_an_erase(chip):
    tmp, tool, _, _, flash = chip
    data = os.urandom(0x40000)
    flash.write_bytes(data)
    path = run(picochip.backup, params(tmp, tool))["path"]
    meta = json.loads(open(os.path.join(path, "meta.json")).read())
    meta["chip"]["chip_id"] = "0A0B0C0D0E0F1011"
    open(os.path.join(path, "meta.json"), "w").write(json.dumps(meta))
    with pytest.raises(common.FlashError, match="has no backup of this chip"):
        picochip.erase(Module(), params(tmp, tool))
    assert flash.read_bytes() == data


def test_a_chip_whose_contents_are_not_wanted_is_erased_when_said_so(chip):
    tmp, tool, _, _, flash = chip
    flash.write_bytes(os.urandom(0x40000))
    run(picochip.erase, params(tmp, tool, without_backup=True))
    assert flash.read_bytes() == b"\xff" * 0x40000


def test_a_whole_chip_erase_with_no_chip_id_in_the_registry_is_refused(chip):
    tmp, tool, _, _, flash = chip
    data = os.urandom(0x40000)
    flash.write_bytes(data)
    with pytest.raises(common.FlashError, match="no chip id"):
        picochip.erase(Module(), params(tmp, tool, chip_id=None, without_backup=True))
    assert flash.read_bytes() == data


# ---- the region level ------------------------------------------------------------------
from test_picoboot import uf2  # noqa: E402
from ansible_collections.rxrbench.core.plugins.module_utils.flash import region_flow  # noqa: E402

APP = bytes(range(256)) * 16                       # 4 KB of image at the start of flash
STORAGE, STORAGE_SIZE = 0x30000, 0x2000            # two sectors of config, as flash.json says


def staged(tmp):
    """An image directory as `stage_raw --flash-map` makes it: the UF2, its manifest, flash.json."""
    from ansible_collections.rxrbench.core.plugins.module_utils.flash import picoboot
    d = tmp / "zapp"
    d.mkdir(exist_ok=True)
    blocks, _ = picoboot.uf2_blocks(uf2(d / "app.uf2", APP))
    (d / "manifest.json").write_text(json.dumps({
        "family": "zephyr_settings", "format": "uf2", "file": "app.uf2", "board_tag": "kit",
        "hash": picoboot.payload_hash(blocks)}))
    (d / "flash.json").write_text(json.dumps({"map": {"STORAGE": hex(STORAGE),
                                                      "STORAGE_SIZE": hex(STORAGE_SIZE)}}))
    return str(d)


def board(flash, runs=True):
    """A chip running the image (or not) with config of its own in the storage region."""
    data = bytearray(os.urandom(0x40000))
    if runs:
        data[:len(APP)] = APP
    flash.write_bytes(bytes(data))
    return data


@pytest.fixture
def zfamily():
    from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
    familyspec.load({"zephyr_settings": {"regions": [["storage", "STORAGE"]]}})


def test_the_settings_region_is_read_from_the_image_s_map_off_the_chip_that_runs_it(chip, zfamily):
    tmp, tool, set_state, _, flash = chip
    data = board(flash)
    img = staged(tmp)
    r = run(access.backup, params(tmp, tool, image=img, flasher="picoboot"))
    got = open(os.path.join(r["path"], "storage.bin"), "rb").read()
    assert got == bytes(data[STORAGE:STORAGE + STORAGE_SIZE])
    meta = json.loads(open(os.path.join(r["path"], "meta.json")).read())
    assert meta["chip_id"] == CHIP and meta["route"] == "picoboot" and meta["regions"][0]["offset"] == hex(STORAGE)
    assert json.loads((tmp / "state.json").read_text())["boot"] is False, "back in its app"


def test_a_board_that_does_not_run_the_image_is_refused_before_anything_is_read(chip, zfamily):
    tmp, tool, _, _, flash = chip
    board(flash, runs=False)
    img = staged(tmp)
    with pytest.raises(common.FlashError, match="does not run this image"):
        run(access.backup, params(tmp, tool, image=img, flasher="picoboot"))
    assert not os.path.exists(tmp / "backups" / "pog1")


def test_a_restore_writes_the_sectors_that_differ_and_must_read_back_as_the_backup(chip, zfamily):
    tmp, tool, _, _, flash = chip
    data = board(flash)
    img = staged(tmp)
    regions = region_flow.regions_of(img)
    path = run(access.backup, params(tmp, tool, image=img, flasher="picoboot"))["path"]
    now = bytearray(flash.read_bytes())
    now[STORAGE + 0x1000:STORAGE + 0x1010] = b"\x00" * 16        # one setting changed since
    now[0x20000:0x20010] = b"\x55" * 16                            # and something outside it
    flash.write_bytes(bytes(now))
    plan = run(access.restore, params(tmp, tool, image=img, flasher="picoboot", backup=path), check_mode=True)
    assert plan["changed"] and "storage (1 of 2 sectors)" in plan["msg"], plan
    r = run(access.restore, params(tmp, tool, image=img, flasher="picoboot", backup=path))
    after = flash.read_bytes()
    assert after[STORAGE:STORAGE + STORAGE_SIZE] == bytes(data[STORAGE:STORAGE + STORAGE_SIZE])
    assert after[0x20000:0x20010] == b"\x55" * 16, "nothing outside the config region is written"
    assert r["changed"]
    again = run(access.restore, params(tmp, tool, image=img, flasher="picoboot", backup=path))
    assert again["changed"] is False and "already" in again["msg"]


def test_another_chips_settings_are_refused(chip, zfamily):
    tmp, tool, _, _, flash = chip
    board(flash)
    img = staged(tmp)
    regions = region_flow.regions_of(img)
    path = run(access.backup, params(tmp, tool, image=img, flasher="picoboot"))["path"]
    meta = json.loads(open(os.path.join(path, "meta.json")).read())
    meta["chip_id"] = "0A0B0C0D0E0F1011"
    open(os.path.join(path, "meta.json"), "w").write(json.dumps(meta))
    with pytest.raises(common.FlashError, match="read from chip 0A0B0C0D0E0F1011"):
        run(access.restore, params(tmp, tool, image=img, flasher="picoboot", backup=path))


def test_a_region_erase_needs_a_region_named_and_lists_them(chip, zfamily):
    tmp, tool, _, _, flash = chip
    data = board(flash)
    img = staged(tmp)
    with pytest.raises(common.FlashError, match=r"name the config region.*storage \(8192 B at 0x30000\).*erase-chip"):
        access.erase(Module(), params(tmp, tool, image=img, flasher="picoboot"))
    assert flash.read_bytes() == bytes(data), "nothing erased"


def test_a_region_erase_on_picoboot_erases_that_region_and_nothing_else(chip, zfamily):
    tmp, tool, _, _, flash = chip
    data = board(flash)
    img = staged(tmp)
    plan = run(access.erase, params(tmp, tool, image=img, flasher="picoboot", region="storage"),
               check_mode=True)
    assert plan["region"] == {"name": "storage", "offset": "0x30000", "size": STORAGE_SIZE}
    assert flash.read_bytes() == bytes(data)
    run(access.erase, params(tmp, tool, image=img, flasher="picoboot", region="storage"))
    after = flash.read_bytes()
    assert after[STORAGE:STORAGE + STORAGE_SIZE] == b"\xff" * STORAGE_SIZE
    assert after[:STORAGE] == bytes(data[:STORAGE])
    assert after[STORAGE + STORAGE_SIZE:] == bytes(data[STORAGE + STORAGE_SIZE:])
