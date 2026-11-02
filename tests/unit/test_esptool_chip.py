"""The esptool chip route's flow (flash/esptool_chip.py): a whole ESP32 read through its ROM
loader to a backup, and written back from one, against a fake chip and a fake broker. Its
helpers have their own tests (test_chip.py); this pins what the route does with them, so moving
it onto the one chip flow (docs/ARCHITECTURE.md#chip-and-region-flows) keeps its behaviour: a route supplies only its
session, and the flow is the same for every route."""
import gzip
import json
import os

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import esppart
from ansible_collections.rxrbench.core.plugins.module_utils.flash import chip, common, esptool_chip

MAC, OTHER = "aa:bb:cc:00:11:22", "aa:bb:cc:00:11:23"
SIZE, BLOCK = 0x100000, 0x10000
DEVICE = {"family": "espfam", "build": "abcd1234", "version": "1.0"}


class Exit(Exception):
    pass


class Fail(Exception):
    pass


class Module:
    def __init__(self, check_mode=False):
        self.check_mode, self.result = check_mode, None

    def exit_json(self, **kw):
        self.result = kw
        raise Exit()

    def fail_json(self, **kw):
        self.result = kw
        raise Fail(kw["msg"])


def call(fn, *a):
    with pytest.raises(Exit):
        fn(*a)
    return a[0].result


def refused(fn, *a):
    """The message a route refuses or fails with, however it ends the run."""
    with pytest.raises((Fail, common.FlashError)) as e:
        fn(*a)
    return str(e.value)


class Board:
    def __init__(self, rig):
        self.rig = rig

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def release(self, ttl=None):
        self.rig.released += 1

    def reacquire(self):
        self.rig.reacquired += 1


class Rig:
    """A chip of SIZE bytes with MAC `mac` behind a broker that answers as `device`."""

    def __init__(self):
        self.mac, self.flash = MAC, bytearray(os.urandom(SIZE))
        self.flash[esppart.OFFSET:esppart.OFFSET + esppart.LENGTH] = b"\xff" * esppart.LENGTH
        self.device, self.after, self.on_chip_manager = dict(DEVICE), dict(DEVICE), None
        self.released = self.reacquired = self.waited = 0
        self.sessions, self.writes = [], 0

    def facts(self):
        return {"mac": self.mac, "size": SIZE, "flash_size": f"{SIZE >> 20}MB", "chip": "esp32s3"}

    def info(self, tools, port):
        self.sessions.append("info")
        return self.facts()

    def read(self, tools, port, path):
        self.sessions.append("read")
        with open(path, "wb") as f:
            f.write(self.flash)
        return {**self.facts(), "blocks": SIZE // BLOCK, "read_blocks": 3, "seconds": 1.0}

    def _differing(self, path):
        data = open(path, "rb").read()
        return [i for i in range(0, SIZE, BLOCK) if data[i:i + BLOCK] != self.flash[i:i + BLOCK]]

    def differ(self, tools, port, path):
        self.sessions.append("diff")
        return {**self.facts(), "differing_blocks": len(self._differing(path)),
                "blocks": SIZE // BLOCK}

    def write(self, tools, port, path):
        self.sessions.append("write")
        self.writes += 1
        n = len(self._differing(path))
        self.flash[:] = open(path, "rb").read()
        return {**self.facts(), "written_blocks": n, "blocks": SIZE // BLOCK, "seconds": 2.0}

    def erase(self, tools, port, mac):
        self.sessions.append(f"erase {mac}")
        if mac.lower() != self.mac:
            raise common.FlashError(f"the chip on this port has MAC {self.mac}, the board's is {mac}")
        self.flash[:] = b"\xff" * SIZE
        return {**self.facts(), "blocks": SIZE // BLOCK, "erased": True, "seconds": 3.0}

    def read_board(self, b, family):
        if self.device is None:
            raise common.FlashError("the board answered nothing")
        return dict(self.device)

    def wait_back(self, b, family, settle):
        self.waited += 1
        return dict(self.after)


@pytest.fixture
def rig(monkeypatch):
    r = Rig()
    monkeypatch.setattr(esptool_chip.broker_client, "board_or_none", lambda *a, **k: Board(r))
    monkeypatch.setattr(common, "read_board", r.read_board)
    monkeypatch.setattr(common, "wait_back", r.wait_back)
    monkeypatch.setattr(common, "esp_port", lambda *_a, **_k: ("/dev/ttyUSB9", None))
    monkeypatch.setattr(common, "read_manager", lambda *_a, **_k: r.on_chip_manager)
    for name in ("info", "read", "differ", "write", "erase"):
        monkeypatch.setattr(chip, name, getattr(r, name))
    return r


def params(tmp_path, **kw):
    return {"board": "e1", "family": "espfam", "dest": str(tmp_path / "backups"),
            "tools": {}, "chip": "esp32s3", "socket": None, "settle": 1.0, "snapshot": None,
            "flasher": "esptool", "app_manager_overwrite": False, "apply_allowed": True, **kw}


def backed_up(rig, tmp_path, **kw):
    return call(esptool_chip.backup, Module(), params(tmp_path, **kw))["path"]


# ---- backup ---------------------------------------------------------------------------------

def test_a_backup_is_the_whole_chip_with_the_chips_mac_and_what_the_board_ran(rig, tmp_path):
    got = call(esptool_chip.backup, Module(), params(tmp_path, snapshot="before"))
    meta = json.load(open(os.path.join(got["path"], "meta.json")))
    assert meta["kind"] == "chip" and meta["board"] == "e1" and meta["size"] == SIZE
    assert meta["chip"] == {"mac": MAC, "flash_size": "1MB", "chip": "esp32s3"}
    assert meta["device"] == DEVICE and meta["came_back_as"] == DEVICE
    assert meta["device_unread"] is None and meta["app_manager"] is None
    assert meta["read_blocks"] == 3 and meta["blocks"] == SIZE // BLOCK
    assert meta["family"] == "espfam" and meta["snapshot"] == "before"
    assert meta.get("route") in (None, "esptool")
    gz = os.path.join(got["path"], "flash.bin.gz")
    with gzip.open(gz) as g:
        assert g.read() == bytes(rig.flash)
    assert oct(os.stat(gz).st_mode & 0o777) == "0o600"
    assert not os.path.exists(os.path.join(got["path"], "flash.bin"))
    assert os.path.realpath(os.path.join(tmp_path, "backups", "e1", "snapshot", "before")) == \
        os.path.realpath(got["path"])
    assert got["chip"] == meta["chip"] and got["device"] == DEVICE and got["changed"]
    assert got["sha256"] == meta["sha256"] and got["snapshot"] == "before"
    assert rig.released == 1 and rig.waited == 1 and rig.sessions == ["read"]


def test_a_board_that_answers_nothing_is_still_backed_up_and_only_given_its_console_back(
        rig, tmp_path):
    rig.device = None
    meta = json.load(open(os.path.join(backed_up(rig, tmp_path), "meta.json")))
    assert meta["device"] is None and "answered nothing" in meta["device_unread"]
    assert meta["came_back_as"] is None and rig.waited == 0 and rig.reacquired == 1


def test_a_board_that_comes_back_as_something_else_fails_the_backup(rig, tmp_path):
    rig.after = {**DEVICE, "build": "ffff0000"}
    assert "came back as" in refused(esptool_chip.backup, Module(), params(tmp_path))


def test_a_plan_reads_nothing_and_an_unknown_chip_or_bad_snapshot_name_is_refused(rig, tmp_path):
    got = call(esptool_chip.backup, Module(check_mode=True), params(tmp_path))
    assert got["changed"] is False and rig.sessions == [] and rig.released == 0
    assert "chip is not known" in refused(esptool_chip.backup, Module(), params(tmp_path, chip=None))
    assert "a snapshot name" in refused(esptool_chip.backup, Module(), params(tmp_path, snapshot="../x"))
    assert rig.sessions == []


def test_the_app_manager_the_chip_carries_is_recorded(rig, tmp_path, monkeypatch):
    mgr = {"label": "factory", "offset": 0x10000, "size": 0x100000, "subtype": 0,
           "apps": [{"label": "ota_0"}], "data": []}
    monkeypatch.setattr(esppart, "manager", lambda rows: mgr)
    meta = json.load(open(os.path.join(backed_up(rig, tmp_path), "meta.json")))
    assert meta["app_manager"] == mgr


# ---- restore --------------------------------------------------------------------------------

def test_a_plan_says_how_many_blocks_differ_and_writes_nothing(rig, tmp_path):
    path = backed_up(rig, tmp_path)
    rig.flash[0:BLOCK * 2] = b"\0" * (BLOCK * 2)
    rig.waited = rig.reacquired = 0
    got = call(esptool_chip.restore, Module(check_mode=True), params(tmp_path), path)
    assert got["changed"] is True and got["differing_blocks"] == 2 and got["blocks"] == 16
    assert rig.waited == 0 and rig.reacquired == 1, "a plan writes nothing: only the console back"
    assert got["msg"] == f"would be written to {MAC} (1MB)"
    assert got["app_manager"] == {"chip": "no app manager", "backup": "no app manager"}
    assert got["recorded"] == DEVICE and got["path"] == path and rig.writes == 0


def test_a_plan_of_a_chip_that_holds_the_backup_changes_nothing(rig, tmp_path):
    path = backed_up(rig, tmp_path)
    got = call(esptool_chip.restore, Module(check_mode=True), params(tmp_path), path)
    assert got["changed"] is False and got["differing_blocks"] == 0


def test_a_restore_writes_the_differing_blocks_and_the_board_must_come_back_as_recorded(
        rig, tmp_path):
    path = backed_up(rig, tmp_path)
    before = bytes(rig.flash)
    rig.flash[0:BLOCK] = b"\0" * BLOCK
    rig.sessions.clear()
    got = call(esptool_chip.restore, Module(), params(tmp_path), path)
    assert bytes(rig.flash) == before and got["changed"] is True
    assert got["written_blocks"] == 1 and got["blocks"] == 16 and got["write_seconds"] == 2.0
    assert got["came_back_as"] == DEVICE and got["recorded"] == DEVICE
    assert "would" not in got.get("msg", ""), "a write is not reported as a plan"
    assert "write" in rig.sessions and "diff" not in rig.sessions, "one write session, no diff first"
    assert not os.path.exists(os.path.join(path, "flash.restore.bin"))
    rig.after = {**DEVICE, "build": "ffff0000"}
    assert "came back as" in refused(esptool_chip.restore, Module(), params(tmp_path), path)


def test_a_restore_is_refused_for_another_board_chip_size_or_a_damaged_image(rig, tmp_path):
    path = backed_up(rig, tmp_path)
    assert "not 'e2'" in refused(esptool_chip.restore, Module(), params(tmp_path, board="e2"), path)
    rig.mac = OTHER
    assert "two boards one node" in refused(esptool_chip.restore, Module(), params(tmp_path), path)
    rig.mac = MAC
    meta_path = os.path.join(path, "meta.json")
    meta = json.load(open(meta_path))
    json.dump({**meta, "chip": {**meta["chip"], "flash_size": "16MB"}}, open(meta_path, "w"))
    assert "16MB chip" in refused(esptool_chip.restore, Module(), params(tmp_path), path)
    json.dump(meta, open(meta_path, "w"))
    with gzip.open(os.path.join(path, "flash.bin.gz"), "wb") as g:
        g.write(b"\0" * SIZE)
    msg = refused(esptool_chip.restore, Module(), params(tmp_path), path)
    assert "SHA-256" in msg or "does not hash" in msg, msg
    assert rig.writes == 0 and not os.path.exists(os.path.join(path, "flash.restore.bin"))


def test_a_backup_from_before_routes_were_recorded_still_goes_back(rig, tmp_path):
    path = backed_up(rig, tmp_path)
    meta_path = os.path.join(path, "meta.json")
    meta = json.load(open(meta_path))
    meta.pop("route", None)
    json.dump(meta, open(meta_path, "w"))
    rig.flash[0:BLOCK] = b"\0" * BLOCK
    assert call(esptool_chip.restore, Module(), params(tmp_path), path)["written_blocks"] == 1


def test_a_backup_without_the_app_manager_the_chip_carries_needs_the_layouts_word(rig, tmp_path):
    path = backed_up(rig, tmp_path)
    rig.on_chip_manager = {"label": "factory", "offset": 0x10000, "apps": [{"label": "ota_0"}]}
    assert "orphan every installed app" in refused(
        esptool_chip.restore, Module(), params(tmp_path), path)
    rig.flash[0:BLOCK] = b"\0" * BLOCK
    got = call(esptool_chip.restore, Module(), params(tmp_path, app_manager_overwrite=True), path)
    assert got["written_blocks"] == 1 and got["app_manager"]["backup"] == "no app manager"


def test_a_board_that_answered_nothing_when_backed_up_is_only_given_its_console_back(
        rig, tmp_path):
    rig.device = None
    path = backed_up(rig, tmp_path)
    rig.flash[0:BLOCK] = b"\0" * BLOCK
    rig.waited = rig.reacquired = 0
    got = call(esptool_chip.restore, Module(), params(tmp_path), path)
    assert "came_back_as" not in got and rig.waited == 0 and rig.reacquired == 1


# ---- the whole chip erased ------------------------------------------------------------------

def test_a_whole_chip_erase_needs_the_registrys_mac(rig, tmp_path):
    before = bytes(rig.flash)
    why = refused(esptool_chip.erase, Module(), params(tmp_path, without_backup=True))
    assert "holds no MAC for this chip" in why and rig.sessions == [] and bytes(rig.flash) == before


def test_another_chip_on_the_port_is_not_erased(rig, tmp_path):
    before = bytes(rig.flash)
    why = refused(esptool_chip.erase, Module(), params(tmp_path, mac=OTHER, without_backup=True))
    assert f"has MAC {MAC}, the registry's is {OTHER}" in why
    assert rig.sessions == ["info"] and bytes(rig.flash) == before


def test_a_whole_chip_erase_needs_a_backup_of_this_chip_and_is_made_with_its_mac(rig, tmp_path):
    before = bytes(rig.flash)
    why = refused(esptool_chip.erase, Module(), params(tmp_path, mac=MAC))
    assert "no backup of this chip" in why and "just backup <layout> e1" in why
    assert bytes(rig.flash) == before
    path = backed_up(rig, tmp_path)
    plan = call(esptool_chip.erase, Module(check_mode=True), params(tmp_path, mac=MAC))
    assert plan["backups"] == [os.path.basename(path)] and f"mac {MAC}" in plan["msg"]
    assert bytes(rig.flash) == before, "a plan erases nothing"
    rig.sessions.clear()
    rig.released = rig.reacquired = 0
    done = call(esptool_chip.erase, Module(), params(tmp_path, mac=MAC))
    assert done["changed"] and rig.sessions == ["info", f"erase {MAC}"]
    assert bytes(rig.flash) == b"\xff" * SIZE
    assert rig.released == 1 and rig.reacquired == 1, "the console let go for the erase and taken back"


def test_a_backup_of_another_chip_does_not_count_for_an_erase(rig, tmp_path):
    path = backed_up(rig, tmp_path)
    meta = json.load(open(os.path.join(path, "meta.json")))
    meta["chip"]["mac"] = OTHER
    json.dump(meta, open(os.path.join(path, "meta.json"), "w"))
    assert "no backup of this chip" in refused(esptool_chip.erase, Module(), params(tmp_path, mac=MAC))
