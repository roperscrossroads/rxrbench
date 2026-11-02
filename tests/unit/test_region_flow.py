"""One region flow per verb over any route (docs/ARCHITECTURE.md#chip-and-region-flows): the
flow with a fake route, the esptool route against a fake esptool, and the check that no module
holds a route's code. The esp-image and picoboot routes have their own tests
(test_esp_image_regions.py, test_picochip.py)."""
import json
import os
import re

import pytest

from tests.support.requirement import breach
from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.module_utils.flash import (
    access, chip, common, esptool_regions, region_flow)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULES = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core",
                       "plugins", "modules")

MAC = "aa:bb:cc:00:11:22"
FS, NVS = (0x90000, 0x1000), (0x9000, 0x800)


class Exit(Exception):
    pass


class Module:
    def __init__(self, check_mode=False):
        self.check_mode, self.result = check_mode, None

    def exit_json(self, **kw):
        self.result = kw
        raise Exit()


def call(fn, *a):
    with pytest.raises(Exit):
        fn(*a)
    return a[0].result


@pytest.fixture
def espfam():
    familyspec.load({"espfam": {"regions": [["fs", "FS"], ["nvs", "NVS"]]}})


def staged(tmp_path):
    d = tmp_path / "img"
    d.mkdir()
    (d / "app.bin").write_bytes(b"app")
    (d / "manifest.json").write_text(json.dumps({
        "family": "espfam", "format": "esp-parts", "file": "app.bin", "hash": "abcd1234ef",
        "build": "abcd1234", "version": "1.0"}))
    (d / "flash.json").write_text(json.dumps({"map": {
        "FS": hex(FS[0]), "FS_SIZE": hex(FS[1]), "NVS": hex(NVS[0]), "NVS_SIZE": hex(NVS[1])}}))
    return str(d)


# ---- the flow, over a fake route ------------------------------------------------------------

class FakeRoute(region_flow.Route):
    name = "fake"

    def __init__(self, p, flash, chip_id="C1", fail_read=False):
        super().__init__(p)
        self.flash, self.chip_id, self.fail_read, self.log = flash, chip_id, fail_read, []

    def chip(self):
        return {"chip_id": self.p.get("chip_id")}

    def session(self, verb, regions, name=None):
        route = self

        class S(region_flow.Session):
            tag, device = "t1", {"family": "espfam"}

            def __enter__(self):
                route.log.append(("enter", verb))
                self.chip = {"chip_id": route.chip_id}
                return self

            def __exit__(self, *exc):
                route.log.append(("leave", verb))
                self.result = {"left": True}
                return False

            def read(self, regions, out):
                if route.fail_read:
                    raise common.FlashError("the read failed")
                for n, off, size in regions:
                    with open(os.path.join(out, f"{n}.bin"), "wb") as f:
                        f.write(bytes(route.flash[off:off + size]))

            def write(self, parts, meta):
                for x in parts:
                    data = open(x["file"], "rb").read()
                    route.flash[x["offset"]:x["offset"] + len(data)] = data
                    route.log.append(("write", x["name"]))

            def erase(self, name, offset, size):
                route.flash[offset:offset + size] = b"\xff" * size

        return S(self, verb)


def fake_params(tmp_path, image, **kw):
    return {"board": "b1", "image": image, "dest": str(tmp_path / "backups"), "chip_id": "C1",
            "apply_allowed": True, **kw}


def test_one_flow_backs_up_records_the_chip_and_restores_through_any_route(tmp_path, espfam):
    image = staged(tmp_path)
    regions = region_flow.regions_of(image)
    flash = bytearray(os.urandom(0x100000))
    route = FakeRoute(fake_params(tmp_path, image), flash)
    got = call(region_flow.backup, Module(), route.p, route, regions)
    meta = json.load(open(os.path.join(got["path"], "meta.json")))
    assert meta["route"] == "fake" and meta["chip_id"] == "C1" and meta["left"] is True
    assert os.path.basename(got["path"]).endswith("-t1")
    saved = bytes(flash[FS[0]:FS[0] + FS[1]])
    flash[FS[0]:FS[0] + 4] = b"\0\0\0\0"
    plan = call(region_flow.restore, Module(check_mode=True), route.p, route, got["path"], regions)
    assert plan["changed"] and "fs (4096 B at 0x90000)" in plan["msg"]
    assert ("write", "fs") not in route.log, "a plan writes nothing"
    r = call(region_flow.restore, Module(), route.p, route, got["path"], regions)
    assert bytes(flash[FS[0]:FS[0] + FS[1]]) == saved and r["left"] is True


def test_a_backup_from_another_chip_is_refused_before_the_board_is_reached(tmp_path, espfam):
    image = staged(tmp_path)
    regions = region_flow.regions_of(image)
    route = FakeRoute(fake_params(tmp_path, image), bytearray(0x100000))
    got = call(region_flow.backup, Module(), route.p, route, regions)
    other = FakeRoute(fake_params(tmp_path, image, chip_id="C2"), bytearray(0x100000))
    with pytest.raises(common.FlashError, match="read from chip C1, this board is chip C2"):
        region_flow.restore(Module(), other.p, other, got["path"], regions)
    assert other.log == []


def test_a_routes_backup_that_records_no_chip_is_refused(tmp_path, espfam):
    image = staged(tmp_path)
    regions = region_flow.regions_of(image)
    route = FakeRoute(fake_params(tmp_path, image), bytearray(0x100000))
    path = call(region_flow.backup, Module(), route.p, route, regions)["path"]
    meta = json.load(open(os.path.join(path, "meta.json")))
    del meta["chip_id"]
    json.dump(meta, open(os.path.join(path, "meta.json"), "w"))
    with pytest.raises(common.FlashError, match="records no chip"):
        region_flow.restore(Module(), route.p, route, path, regions)


def test_a_backup_from_before_routes_recorded_chips_is_tied_to_its_board_and_says_so(
        tmp_path, espfam):
    image = staged(tmp_path)
    regions = region_flow.regions_of(image)
    route = FakeRoute(fake_params(tmp_path, image), bytearray(0x100000))
    path = call(region_flow.backup, Module(), route.p, route, regions)["path"]
    meta = json.load(open(os.path.join(path, "meta.json")))
    for k in ("route", "chip_id"):
        del meta[k]
    json.dump(meta, open(os.path.join(path, "meta.json"), "w"))
    assert call(region_flow.restore, Module(), route.p, route, path, regions)["chip_checked"] is False


def test_a_failed_read_leaves_no_backup_directory(tmp_path, espfam):
    image = staged(tmp_path)
    route = FakeRoute(fake_params(tmp_path, image), bytearray(0x100000), fail_read=True)
    with pytest.raises(common.FlashError, match="the read failed"):
        region_flow.backup(Module(), route.p, route, region_flow.regions_of(image))
    assert os.listdir(tmp_path / "backups" / "b1") == []
    assert route.log == [("enter", "backup"), ("leave", "backup")]


def test_an_erase_takes_a_settings_region_only_and_a_write_needs_the_go_ahead(tmp_path, espfam):
    image = staged(tmp_path)
    regions = region_flow.regions_of(image)
    flash = bytearray(0x100000)
    route = FakeRoute(fake_params(tmp_path, image), flash)
    with pytest.raises(common.FlashError, match="never erased"):
        region_flow.erase(Module(), route.p, route, regions, "app0")
    no = FakeRoute(fake_params(tmp_path, image, apply_allowed=False), flash)
    with pytest.raises(common.FlashError, match="bench_apply_allowed"):
        region_flow.erase(Module(), no.p, no, regions, "nvs")
    call(region_flow.erase, Module(), route.p, route, regions, "nvs")
    assert bytes(flash[NVS[0]:NVS[0] + NVS[1]]) == b"\xff" * NVS[1]


# ---- the esptool route, against a fake esptool ----------------------------------------------

class Board:
    """A broker that answers as the board runs `device`."""

    def __init__(self, *a, **k):
        self.released = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def release(self, ttl=None):
        self.released += 1


class Esptool:
    """common.tool for esptool, on a chip of `flash` with MAC `mac`."""

    def __init__(self, mac=MAC, size=0x100000):
        self.mac, self.flash, self.calls = mac, bytearray(os.urandom(size)), []

    def __call__(self, argv, timeout, **_kw):
        self.calls.append(argv)
        head = f"Connected to ESP32 on {argv[argv.index('--port') + 1]}:\nMAC:                {self.mac}\n"
        if "read-flash" in argv:
            i = argv.index("read-flash")
            off, size, path = int(argv[i + 1], 16), int(argv[i + 2], 16), argv[i + 3]
            open(path, "wb").write(bytes(self.flash[off:off + size]))
            return 0, head + "Read done.\n"
        if "read-mac" in argv:
            return 0, head + f"MAC:                {self.mac}\n"
        if "write_flash" in argv:
            args = argv[argv.index("--flash_size") + 2:]
            for off, path in zip(args[::2], args[1::2]):
                data = open(path, "rb").read()
                self.flash[int(off, 16):int(off, 16) + len(data)] = data
            return 0, head + "Hash of data verified.\n" * (len(args) // 2)
        if "erase_region" in argv:
            i = argv.index("erase_region")
            off, size = int(argv[i + 1], 16), int(argv[i + 2], 16)
            self.flash[off:off + size] = b"\xff" * size
            return 0, head
        raise AssertionError(argv)

    def wrote(self):
        return [a for a in self.calls if "write_flash" in a or "erase_region" in a]


@pytest.fixture
def esp(monkeypatch, espfam):
    tool = Esptool()
    device = {"family": "espfam", "hash": "abcd1234ef", "build": "abcd1234", "version": "1.0"}
    monkeypatch.setattr(esptool_regions.broker_client, "Board", Board)
    monkeypatch.setattr(common, "read_board", lambda *_a, **_k: dict(device))
    monkeypatch.setattr(common, "wait_back", lambda *_a, **_k: dict(device))
    monkeypatch.setattr(common, "esp_port", lambda *_a, **_k: ("/dev/ttyUSB9", None))
    monkeypatch.setattr(common, "esp_write_refusals", lambda *_a, **_k: [])
    monkeypatch.setattr(chip, "reset", lambda *_a: None)
    monkeypatch.setattr(common, "tool", tool)
    return tool


def esp_params(tmp_path, image, **kw):
    return {"board": "e1", "image": image, "dest": str(tmp_path / "backups"), "chip": "esp32",
            "mac": MAC, "tools": {}, "socket": None, "settle": 1.0, "flasher": "esptool",
            "overwrite": [], "app_manager_overwrite": False, "wedged": False, "region": None,
            "apply_allowed": True, **kw}


def test_an_esptool_backup_records_the_chips_mac_and_goes_back_only_to_that_chip(tmp_path, esp):
    image = staged(tmp_path)
    got = call(access.backup, Module(), esp_params(tmp_path, image))
    meta = json.load(open(os.path.join(got["path"], "meta.json")))
    assert meta["route"] == "esptool" and meta["mac"] == MAC
    assert meta["came_back_as"]["build"] == "abcd1234"
    saved = bytes(esp.flash[FS[0]:FS[0] + FS[1]])
    esp.flash[FS[0]:FS[0] + 8] = b"\0" * 8
    call(access.restore, Module(), esp_params(tmp_path, image, backup=got["path"]))
    assert bytes(esp.flash[FS[0]:FS[0] + FS[1]]) == saved
    # the same board name, another chip on its port, and no MAC in the registry to catch it
    esp.mac = "aa:bb:cc:00:11:23"
    esp.calls.clear()
    with pytest.raises(common.FlashError, match=f"read from chip {MAC}"):
        access.restore(Module(), esp_params(tmp_path, image, backup=got["path"], mac=None))
    assert esp.wrote() == [], "nothing is written to another chip"


def test_an_esptool_chip_that_is_not_the_registrys_is_refused(tmp_path, esp):
    image = staged(tmp_path)
    esp.mac = "aa:bb:cc:00:11:23"
    with pytest.raises(common.FlashError, match="another board"):
        access.backup(Module(), esp_params(tmp_path, image))
    assert os.listdir(tmp_path / "backups" / "e1") == []


def test_an_esptool_plan_reads_the_console_and_not_the_chip(tmp_path, esp):
    image = staged(tmp_path)
    got = call(access.backup, Module(), esp_params(tmp_path, image))
    esp.calls.clear()
    assert "would read fs" in call(access.backup, Module(check_mode=True),
                                   esp_params(tmp_path, image))["msg"]
    assert call(access.restore, Module(check_mode=True),
                esp_params(tmp_path, image, backup=got["path"]))["changed"]
    assert call(access.erase, Module(check_mode=True), esp_params(tmp_path, image, region="fs"))["region"] == \
        {"name": "fs", "offset": hex(FS[0]), "size": FS[1]}
    assert esp.calls == []


def test_an_esptool_erase_blanks_one_settings_region(tmp_path, esp):
    image = staged(tmp_path)
    call(access.erase, Module(), esp_params(tmp_path, image, region="nvs"))
    assert bytes(esp.flash[NVS[0]:NVS[0] + NVS[1]]) == b"\xff" * NVS[1]
    assert esp.flash[FS[0]:FS[0] + 16] != b"\xff" * 16


def test_the_mac_esptool_prints_is_read_in_both_spellings():
    assert esptool_regions.mac_of("MAC: AA:BB:CC:00:11:22\n") == MAC
    assert esptool_regions.mac_of("Chip type: ESP32\nMAC:                aa:bb:cc:00:11:22\n") == MAC
    assert esptool_regions.mac_of("BASE MAC: aa:bb:cc:00:11:22") == MAC
    assert esptool_regions.mac_of("no mac here") is None


# ---- no module holds a route's code -----------------------------------------------------------

#: esptool's commands, picotool's, the chip_blocks sessions and the probe's tools: a route's,
#: never a flash module's
ROUTE_CODE = re.compile(r"read[-_]flash|write[-_]flash|erase[-_]region|read[-_]mac|picotool"
                        r"|region-(?:read|write|erase)|chip_blocks|swd_chip|openocd")

#: the modules that back up, restore and erase regions or a whole chip
FLASH_MODULES = ("region_backup.py", "region_restore.py", "region_erase.py", "chip_backup.py",
                 "chip_restore.py")


def test_the_flash_modules_hold_no_routes_code():
    found = []
    for name in FLASH_MODULES:
        text = open(os.path.join(MODULES, name)).read()
        start = text.find("from ansible.module_utils")   # past the documentation
        code = text[max(start, 0):]
        found += [f"{name}: names {hit}" for hit in sorted(set(ROUTE_CODE.findall(code)))]
    assert not found, breach(
        found, "docs/ARCHITECTURE.md#chip-and-region-flows: each route's chip access lives in one place, its "
               "route (module_utils/flash/access.py ROUTES and CHIP_ROUTES), and no module's "
               "main() holds it",
        "move the chip access into the route's class and call it through access.backup, "
        "access.restore, access.erase, access.chip_backup, access.chip_restore or access.chip_erase")
