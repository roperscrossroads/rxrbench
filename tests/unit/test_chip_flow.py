"""The one chip flow (flash/chip_flow.py, docs/ARCHITECTURE.md#chip-and-region-flows) against a fake
route whose chip is a file: what every chip route gets from it without a line of its own."""
import gzip
import json
import os
import re

import pytest

from tests.support.requirement import breach
from ansible_collections.rxrbench.core.plugins.module_utils.flash import chip_flow, common

FLASH = os.path.dirname(chip_flow.__file__)
#: every core chip route (access.CHIP_ROUTES, and the sim's)
CHIP_ROUTE_FILES = ("esptool_chip.py", "picochip.py", "simchip.py", "swdchip.py")
#: what only the flow does: compress the image, write its record, link a snapshot
FLOW_CODE = re.compile(r"\bgzip\b|json\.dumps?\(|os\.symlink\(")

SIZE = 4096


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


class FileSession(chip_flow.ChipSession):
    def __enter__(self):
        self.route.entered += 1
        if self.route.absent:
            raise common.FlashError("the chip does not answer")
        return self

    def read(self, raw):
        with open(self.route.flash, "rb") as f, open(raw, "wb") as o:
            o.write(f.read()[:self.route.short or None])
        return SIZE, {"chip_id": self.route.chip_id}, {"extra": 1}

    def refusals(self, meta, size, raw):
        return [] if size == SIZE else [f"the backup is {size} B, the chip has {SIZE} B"]

    def differ(self, raw):
        with open(raw, "rb") as a, open(self.route.flash, "rb") as b:
            same = a.read() == b.read()
        return not same, {"msg": "differs" if not same else "the chip holds the backup already"}

    def write(self, raw):
        self.route.writes += 1
        if not self.route.broken:
            with open(raw, "rb") as a, open(self.route.flash, "wb") as b:
                b.write(a.read())
        with open(raw, "rb") as a, open(self.route.flash, "rb") as b:
            if a.read() != b.read():
                raise common.FlashError("after the write the chip does not hash to the backup")
        return {"msg": "written"}


class FileRoute(chip_flow.ChipRoute):
    name = "file"

    def __init__(self, p, flash, chip_id="c1", takes_unrouted=False):
        super().__init__(p)
        self.flash, self.chip_id, self.takes_unrouted = flash, chip_id, takes_unrouted
        self.entered = self.writes = 0
        self.absent = self.broken = False
        self.short = 0

    def refusals(self, meta, size):
        was = (meta.get("chip") or {}).get("chip_id")
        return [] if was == self.chip_id else [f"the backup was read from chip {was}, this board is chip {self.chip_id}"]

    def session(self, verb):
        return FileSession(self, verb)


@pytest.fixture
def rig(tmp_path):
    flash = tmp_path / "flash.bin"
    flash.write_bytes(os.urandom(SIZE))

    def params(**kw):
        return {"board": "b1", "dest": str(tmp_path / "backups"), "family": "bare",
                "apply_allowed": True, "snapshot": None, **kw}
    params.flash = flash
    return params


def test_a_backup_is_the_whole_chip_gzipped_with_its_record_and_snapshot(rig):
    route = FileRoute(rig(), rig.flash)
    got = call(chip_flow.backup, Module(), rig(snapshot="before"), route)
    meta = json.load(open(os.path.join(got["path"], "meta.json")))
    assert meta["route"] == "file" and meta["kind"] == "chip" and meta["size"] == SIZE
    assert meta["chip"] == {"chip_id": "c1"} and meta["extra"] == 1 and meta["snapshot"] == "before"
    with gzip.open(os.path.join(got["path"], "flash.bin.gz")) as g:
        assert g.read() == rig.flash.read_bytes()
    assert oct(os.stat(os.path.join(got["path"], "flash.bin.gz")).st_mode & 0o777) == "0o600"
    link = os.path.join(rig()["dest"], "b1", "snapshot", "before")
    assert os.path.realpath(link) == os.path.realpath(got["path"])


def test_a_plan_of_a_backup_touches_nothing_and_a_bad_snapshot_name_is_refused(rig):
    route = FileRoute(rig(), rig.flash)
    assert call(chip_flow.backup, Module(check_mode=True), rig(), route)["changed"] is False
    assert route.entered == 0
    with pytest.raises(common.FlashError, match="a snapshot name"):
        chip_flow.backup(Module(), rig(snapshot="../x"), route)


@pytest.mark.parametrize("how", ["absent", "short"])
def test_a_failed_backup_leaves_no_directory(rig, how):
    route = FileRoute(rig(), rig.flash)
    if how == "absent":
        route.absent = True
    else:
        route.short = 100
    with pytest.raises(common.FlashError):
        chip_flow.backup(Module(), rig(), route)
    chip = os.path.join(rig()["dest"], "b1", "chip")
    assert not os.path.isdir(chip) or os.listdir(chip) == []


def test_a_restore_writes_back_only_a_chip_that_differs(rig):
    route = FileRoute(rig(), rig.flash)
    path = call(chip_flow.backup, Module(), rig(), route)["path"]
    before = rig.flash.read_bytes()
    rig.flash.write_bytes(b"\xff" * SIZE)
    plan = call(chip_flow.restore, Module(check_mode=True), rig(), route, path)
    assert plan["changed"] is True and route.writes == 0
    done = call(chip_flow.restore, Module(), rig(), route, path)
    assert done["changed"] and done["msg"] == "written" and rig.flash.read_bytes() == before
    again = call(chip_flow.restore, Module(), rig(), route, path)
    assert again["changed"] is False and route.writes == 1


def test_a_restore_is_refused_for_another_board_route_chip_or_a_damaged_image(rig):
    route = FileRoute(rig(), rig.flash)
    path = call(chip_flow.backup, Module(), rig(), route)["path"]
    with pytest.raises(common.FlashError, match="the backup is of 'b1', not 'b2'"):
        chip_flow.restore(Module(), rig(board="b2"), route, path)
    other = FileRoute(rig(), rig.flash)
    other.name = "other"
    with pytest.raises(common.FlashError, match="read by another route"):
        chip_flow.restore(Module(), rig(), other, path)
    with pytest.raises(common.FlashError, match="read from chip c1, this board is chip c2"):
        chip_flow.restore(Module(), rig(), FileRoute(rig(), rig.flash, chip_id="c2"), path)
    with gzip.open(os.path.join(path, "flash.bin.gz"), "wb") as g:
        g.write(b"\0" * SIZE)
    with pytest.raises(common.FlashError, match="does not hash"):
        chip_flow.restore(Module(), rig(), route, path)
    assert route.entered == 1 and route.writes == 0, "refused before the chip is touched"


def test_a_backup_with_no_route_goes_only_to_a_route_that_takes_one(rig):
    route = FileRoute(rig(), rig.flash)
    path = call(chip_flow.backup, Module(), rig(), route)["path"]
    meta_path = os.path.join(path, "meta.json")
    meta = json.load(open(meta_path))
    del meta["route"]
    json.dump(meta, open(meta_path, "w"))
    with pytest.raises(common.FlashError, match="another route"):
        chip_flow.restore(Module(), rig(), route, path)
    rig.flash.write_bytes(b"\xff" * SIZE)
    assert call(chip_flow.restore, Module(), rig(), FileRoute(rig(), rig.flash, takes_unrouted=True),
                path)["changed"]


def test_a_write_needs_the_go_ahead_and_must_leave_the_chip_as_the_backup(rig):
    route = FileRoute(rig(), rig.flash)
    path = call(chip_flow.backup, Module(), rig(), route)["path"]
    rig.flash.write_bytes(b"\xff" * SIZE)
    with pytest.raises(common.FlashError, match="bench_apply_allowed"):
        chip_flow.restore(Module(), rig(apply_allowed=False), route, path)
    assert route.writes == 0
    route.broken = True
    with pytest.raises(common.FlashError, match="does not hash to the backup"):
        chip_flow.restore(Module(), rig(), route, path)


def test_the_chip_routes_hold_no_copy_of_the_flow():
    found = []
    for name in CHIP_ROUTE_FILES:
        text = open(os.path.join(FLASH, name)).read()
        found += [f"{name}: {hit}" for hit in sorted(set(FLOW_CODE.findall(text)))]
    assert not found, breach(
        found, "docs/ARCHITECTURE.md#chip-and-region-flows at the chip level: one backup and one restore "
               "flow (module_utils/flash/chip_flow.py) for every route; a route supplies only "
               "its session, its chip's identity and its own meta fields",
        "move the step into chip_flow.py, or give the route's session the part that differs "
        "(ChipSession.read, refusals, differ, write)")
