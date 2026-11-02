"""The region level of a board written with a whole merged ESP32 image (a stock release): its
family's data partitions, found in the image's own partition table, read, written back and
erased through the ROM loader, only on the chip the registry names and only when the chip runs
the image (an erase: when its partition table is the image's)."""
import functools
import hashlib
import json
import os

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import chip as chipio
from ansible_collections.rxrbench.core.plugins.module_utils.flash import access, common, esp_image_regions
from ansible_collections.rxrbench.core.plugins.module_utils.flash import region_flow
from tests.unit.esptables import row
from tests.unit.test_esp_image import MAC, SIZE, FakeEsp, fake_write_flash, helper, merged

# a stock release's table: nvs, a factory app, the file system its prefs and key live in
ROWS = [row(1, 0x02, 0x9000, 0x5000, "nvs"), row(0, 0x00, 0x10000, 0x40000, "app0"),
        row(1, 0x82, 0x80000, 0x20000, "spiffs")]
FS, NVS = (0x80000, 0xA0000), (0x9000, 0xE000)


# Core's region level on a stock-style family's whole-image board: the fixture plugin's
# gadget (its data partitions are named as a stock Meshtastic release's are).
pytestmark = pytest.mark.usefixtures("widgets")


def blob(app=b"app-1"):
    return merged(app=app, rows=ROWS)


def staged(tmp_path, data, family="gadget"):
    d = tmp_path / "img"
    d.mkdir(exist_ok=True)
    (d / "m.bin").write_bytes(data)
    (d / "manifest.json").write_text(json.dumps({
        "family": family, "format": "raw-bin", "file": "m.bin", "board_tag": "heltec-v4",
        "hash": hashlib.sha256(data).hexdigest(), "version": "2.8.2.d5f1619"}))
    return str(d)


def chip_with(data, fs=b"prefs+key", nvs=b"bonds"):
    """A chip that runs `data`, with what the firmware wrote in its data partitions."""
    flash = bytearray(data + b"\xff" * (SIZE - len(data)))
    flash[FS[0]:FS[0] + len(fs)] = fs
    flash[NVS[0]:NVS[0] + len(nvs)] = nvs
    return flash


class Esp(FakeEsp):
    def read_flash(self, addr, size):
        return bytes(self.flash[addr:addr + size])

    def erase_region(self, addr, size):
        assert addr % 0x1000 == 0 and size % 0x1000 == 0
        self.flash[addr:addr + size] = b"\xff" * size
        self.writes.append(("erase", addr, size))


# ---- the map ----------------------------------------------------------------------------------

def test_a_stock_releases_settings_are_found_in_its_own_partition_table(tmp_path):
    d = staged(tmp_path, blob())
    assert region_flow.regions_of(d) == [("fs", FS[0], FS[1] - FS[0]), ("nvs", NVS[0], NVS[1] - NVS[0])]
    assert "APP0" not in region_flow.merged_map(d), "an app partition is never a config region"


def test_a_merged_image_with_no_table_names_no_settings(tmp_path):
    d = staged(tmp_path, b"\xff" * 0x20000)
    with pytest.raises(common.FlashError, match="guessed offset"):
        region_flow.regions_of(d)


@pytest.mark.plugins
def test_only_a_whole_image_board_of_a_stock_family_has_the_settings_level():
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families
    assert common.can("esp-image") == ["regions", "chip"]
    for fam in ("meshtastic", "meshcore"):
        plugin = families.plugin(fam)
        assert plugin.config_regions[0] == ("fs", "SPIFFS")
        # its UF2 for an nRF52 or an RP2040 names no regions
        assert plugin.region_flashers == ("esp-image",)


# ---- the host tool ----------------------------------------------------------------------------

def run(cb, esp, monkeypatch, capsys, *argv):
    """The tool's main on a fake chip (esptool's write_flash faked too): its one JSON line."""
    monkeypatch.setattr(cb, "connect", lambda _port: esp)
    monkeypatch.setattr(cb, "region", functools.partial(cb.region, write_flash=fake_write_flash))
    try:
        cb.main(["/dev/ttyUSB0", *argv])
    except SystemExit as e:
        out = capsys.readouterr().out.strip() or str(e.code)
        return json.loads(out.splitlines()[-1])
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.fixture
def cb():
    return helper()


RUNS = "0x0:0x8000,0x8000:0x8c00,0x10000:0x50000"


def test_a_region_is_read_written_back_and_erased_in_one_session_and_checked(tmp_path, cb,
                                                                               monkeypatch, capsys):
    img = tmp_path / "m.bin"
    img.write_bytes(blob())
    esp = Esp(chip_with(blob()))
    out = tmp_path / "fs.bin"
    got = run(cb, esp, monkeypatch, capsys, "region-read", str(img), f"{FS[0]:#x}:{FS[1]:#x}={out}",
              "--runs", RUNS, "--mac", MAC)
    assert got["ok"] and out.read_bytes()[:9] == b"prefs+key" and esp.writes == []
    got = run(cb, esp, monkeypatch, capsys, "region-erase", str(img), f"{FS[0]:#x}:{FS[1]:#x}",
              "--runs", "0x8000:0x8c00", "--mac", MAC)
    assert got["ok"] and bytes(esp.flash[FS[0]:FS[1]]) == b"\xff" * (FS[1] - FS[0])
    assert bytes(esp.flash[NVS[0]:NVS[0] + 5]) == b"bonds", "only the region named is erased"
    got = run(cb, esp, monkeypatch, capsys, "region-write", str(img), f"{FS[0]:#x}:{FS[1]:#x}={out}",
              "--runs", RUNS, "--mac", MAC)
    assert got["ok"] and bytes(esp.flash[FS[0]:FS[0] + 9]) == b"prefs+key"


def test_nothing_is_read_or_written_on_a_chip_that_does_not_run_the_image(tmp_path, cb,
                                                                          monkeypatch, capsys):
    img = tmp_path / "m.bin"
    img.write_bytes(blob())
    esp = Esp(chip_with(blob(app=b"app-2")))      # another app under the same table
    out = tmp_path / "fs.bin"
    got = run(cb, esp, monkeypatch, capsys, "region-read", str(img), f"{FS[0]:#x}:{FS[1]:#x}={out}",
              "--runs", RUNS, "--mac", MAC)
    assert got["ok"] is False and "does not run the image" in got["error"] and not out.exists()
    # an erase asks only for the table: a node whose app no longer starts can be erased
    got = run(cb, esp, monkeypatch, capsys, "region-erase", str(img), f"{FS[0]:#x}:{FS[1]:#x}",
              "--runs", "0x8000:0x8c00", "--mac", MAC)
    assert got["ok"]
    esp = Esp(chip_with(merged(rows=[row(1, 0x82, 0x60000, 0x20000, "spiffs")])))   # another table
    got = run(cb, esp, monkeypatch, capsys, "region-erase", str(img), f"{FS[0]:#x}:{FS[1]:#x}",
              "--runs", "0x8000:0x8c00", "--mac", MAC)
    assert got["ok"] is False and esp.writes == []


def test_another_chip_on_the_port_is_refused_before_a_region_is_touched(tmp_path, cb,
                                                                        monkeypatch, capsys):
    img = tmp_path / "m.bin"
    img.write_bytes(blob())
    esp = Esp(chip_with(blob()), mac="aa:bb:cc:00:11:23")
    out = tmp_path / "fs.bin"
    got = run(cb, esp, monkeypatch, capsys, "region-read", str(img), f"{FS[0]:#x}:{FS[1]:#x}={out}",
              "--runs", RUNS, "--mac", MAC)
    assert "not this board" in got["error"] and not out.exists()


# ---- the modules' side ------------------------------------------------------------------------

class Exit(Exception):
    pass


class Module:
    def __init__(self, check_mode=False):
        self.check_mode = check_mode
        self.result = None

    def exit_json(self, **kw):
        self.result = kw
        raise Exit()


class Rig:
    """chip.blocks runs chip_blocks against an Esp, with the port resolved and resets counted."""

    def __init__(self, monkeypatch, esp):
        self.esp, self.resets, self.cb = esp, 0, helper()
        monkeypatch.setattr(common, "esp_port", lambda *a, **k: ("/dev/ttyUSB0", None))
        monkeypatch.setattr(chipio, "reset", lambda *_a: setattr(self, "resets", self.resets + 1))
        monkeypatch.setattr(chipio, "blocks", self._blocks)

    def _blocks(self, _tools, _port, cmd, path, *args, timeout=900):  # noqa: ARG002
        args, runs = list(args), None
        i = args.index("--mac")
        mac, args = args[i + 1], args[:i] + args[i + 2:]
        if "--runs" in args:
            i = args.index("--runs")
            runs = [self.cb.span(r) for r in args[i + 1].split(",")]
            args = args[:i] + args[i + 2:]
        rest = args
        out = self.cb.info(self.esp)
        why = self.cb.mac_refusal(out, mac)
        if cmd == "match":
            out["mac_ok"] = why is None
            out.update(self.cb.match(self.esp, path, [self.cb.span(r) for r in rest]))
        else:
            if why:
                raise common.FlashError(why)
            got = self.cb.region(self.esp, cmd, path, self.cb.parts_of(cmd, rest), runs,
                                 write_flash=fake_write_flash)
            if got.get("ok") is False:
                raise common.FlashError(f"chip_blocks {cmd} failed (rc 1): {got['error']}")
            out.update(got)
        out["flash_size"] = f"{out['size'] >> 20}MB"
        return out


def params(tmp_path, image, **kw):
    p = {"board": "board9", "image": image, "chip": "esp32", "mac": MAC, "tools": {},
         "flasher": "esp-image", "region": None,
         "socket": str(tmp_path / "none.sock"), "settle": 1.0, "dest": str(tmp_path / "backups"),
         "apply_allowed": True}
    p.update(kw)
    return p


def call(fn, *a):
    try:
        fn(*a)
    except Exit:
        pass
    return a[0].result


def test_a_stock_node_is_backed_up_erased_and_restored_through_its_loader(tmp_path, monkeypatch):
    image = staged(tmp_path, blob())
    regions = region_flow.regions_of(image)
    esp = Esp(chip_with(blob()))
    rig = Rig(monkeypatch, esp)
    p = params(tmp_path, image)
    got = call(access.backup, Module(), p)
    meta = json.loads(open(got["path"] + "/meta.json").read())
    assert meta["route"] == "esp-image" and meta["mac"] == MAC
    assert [r["name"] for r in meta["regions"]] == ["fs", "nvs"]
    assert call(access.erase, Module(check_mode=True), dict(p, region="fs"))["region"]["name"] == "fs"
    assert esp.writes == [], "a plan erases nothing"
    call(access.erase, Module(), dict(p, region="fs"))
    assert bytes(esp.flash[FS[0]:FS[0] + 9]) == b"\xff" * 9
    call(access.restore, Module(), dict(p, backup=got["path"]))
    assert bytes(esp.flash[FS[0]:FS[0] + 9]) == b"prefs+key"
    assert rig.resets == 4, "the board is booted after every session (a plan's read too)"


def test_a_backup_goes_back_only_to_the_chip_it_was_read_from(tmp_path, monkeypatch):
    image = staged(tmp_path, blob())
    regions = region_flow.regions_of(image)
    Rig(monkeypatch, Esp(chip_with(blob())))
    got = call(access.backup, Module(), params(tmp_path, image))
    Rig(monkeypatch, Esp(chip_with(blob()), mac="aa:bb:cc:00:11:23"))
    with pytest.raises(common.FlashError, match="read from chip"):
        access.restore(Module(), params(tmp_path, image, mac="aa:bb:cc:00:11:23",
                                        backup=got["path"]))


def test_a_board_a_release_behind_is_backed_up_by_its_table_and_restored_only_on_the_image(
        tmp_path, monkeypatch):
    """A backup needs only the table to match (it says where the config regions are), so a board one
    release behind its layout can be backed up before its upgrade; the record says only the
    table was compared. A restore still needs the whole image."""
    image = staged(tmp_path, blob(app=b"app-2"))            # the layout's new release
    regions = region_flow.regions_of(image)
    esp = Esp(chip_with(blob(app=b"app-1")))                # the board, a release behind
    Rig(monkeypatch, esp)
    got = call(access.backup, Module(), params(tmp_path, image))
    meta = json.loads(open(got["path"] + "/meta.json").read())
    assert meta["matched"] == "table" and meta["device"] == {"family": "gadget"}
    assert open(got["path"] + "/fs.bin", "rb").read()[:9] == b"prefs+key"
    with pytest.raises(common.FlashError, match="does not run the image"):
        access.restore(Module(), params(tmp_path, image, backup=got["path"]))
    assert esp.writes == []
    Rig(monkeypatch, Esp(chip_with(merged(rows=[row(1, 0x82, 0x60000, 0x20000, "spiffs")]))))
    with pytest.raises(common.FlashError, match="does not run the image"):
        access.backup(Module(), params(tmp_path, image, dest=str(tmp_path / "other")))
    assert not os.listdir(tmp_path / "other" / "board9"), "a refused backup leaves no directory"


def test_no_mac_in_the_registry_reads_and_writes_nothing(tmp_path, monkeypatch):
    image = staged(tmp_path, blob())
    esp = Esp(chip_with(blob()))
    Rig(monkeypatch, esp)
    with pytest.raises(common.FlashError, match="no MAC"):
        access.erase(Module(), params(tmp_path, image, mac=None, region="fs"))
    assert esp.writes == []


def test_an_app_partition_is_never_erased(tmp_path, monkeypatch):
    image = staged(tmp_path, blob())
    Rig(monkeypatch, Esp(chip_with(blob())))
    with pytest.raises(common.FlashError, match="never erased"):
        access.erase(Module(), params(tmp_path, image, region="app0"))
