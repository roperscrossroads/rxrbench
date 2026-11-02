"""A whole chip behind a debug probe backed up and written back (flash/swdchip.py), through the
chip route table (flash/access.py), against a fake host tool: the chip's unique id is the guard,
and the chip's own hash after the write is the proof."""
import json
import os
import stat

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import access, common

UID = "0123456789abcdef01234567"
SIZE = 0x10000

FAKE_TOOL = """#!/usr/bin/env python3
import hashlib, json, os, sys
a = sys.argv[1:]
state = os.environ["CHIP_STATE"]
open(state + ".log", "a").write(" ".join(a) + "\\n")
uid = a[a.index("--uid") + 1]
if uid != os.environ["CHIP_UID"]:
    print(json.dumps({"error": "the chip behind the probe has unique id X: nothing was done"}))
    sys.exit(1)
BASE = 0x08000000
at = int(a[a.index("--at") + 1], 0) if "--at" in a else BASE
off = at - BASE
flash = open(state, "rb").read()
out = {"uid": uid, "flash_size": len(flash), "flash_base": BASE, "idcode": "00006410"}
if a[0] == "write":
    data = open(a[1], "rb").read()
    if os.environ.get("CHIP_BROKEN") != "1":
        flash = flash[:off] + data + flash[off + len(data):]
        open(state, "wb").write(flash)
    out["written"] = True
elif a[0] == "read":
    open(a[1], "wb").write(flash[off:off + int(a[2])])
elif a[0] == "erase":
    # a length: that range; none: the whole chip (its mass erase)
    n = int(a[1], 0) if len(a) > 1 and not a[1].startswith("--") else len(flash) - off
    if os.environ.get("CHIP_BROKEN") != "1":
        open(state, "wb").write(flash[:off] + b"\\xff" * n + flash[off + n:])
    out["erased"] = True
elif a[0] == "sha":
    out["sha256"] = hashlib.sha256(flash[off:off + int(a[1])]).hexdigest()
print(json.dumps(out))
"""


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


def script(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def pill(tmp_path, monkeypatch):
    state = tmp_path / "flash.bin"
    state.write_bytes(os.urandom(SIZE))
    monkeypatch.setenv("CHIP_STATE", str(state))
    monkeypatch.setenv("CHIP_UID", UID)
    tools = {"swd_chip": script(tmp_path / "swd_chip.py", FAKE_TOOL),
             "devreg": script(tmp_path / "devreg.py",
                              "import sys\nprint('1-1.3' if '--busid' in sys.argv else '0483:3748')\n")}

    def params(**kw):
        return {"board": "pill", "flasher": "swd", "dest": str(tmp_path / "backups"),
                "tools": tools, "family": "bare", "snapshot": None, "run_dir": None,
                "apply_allowed": True,
                "route": {"probe": "probe1", "chip": "STM32F103C8", "uid": UID}, **kw}
    params.state = state
    params.log = tmp_path / "flash.bin.log"
    return params


def written(pill):
    return [x for x in pill.log.read_text().splitlines() if x.startswith("write")]


def test_a_chip_behind_a_probe_is_backed_up_whole_and_written_back(pill):
    before = pill.state.read_bytes()
    got = call(access.chip_backup, Module(), pill())
    meta = json.load(open(os.path.join(got["path"], "meta.json")))
    assert meta["route"] == "swd" and meta["chip"]["uid"] == UID and meta["size"] == SIZE
    pill.state.write_bytes(b"\xff" * SIZE)                      # another firmware since
    plan = call(access.chip_restore, Module(check_mode=True), pill(), got["path"])
    assert plan["changed"] and written(pill) == [], "a plan writes nothing"
    call(access.chip_restore, Module(), pill(), got["path"])
    assert pill.state.read_bytes() == before
    again = call(access.chip_restore, Module(), pill(), got["path"])
    assert again["changed"] is False and len(written(pill)) == 1, "a chip that holds it is not written"


def test_another_chips_backup_is_refused_before_anything_is_written(pill, monkeypatch):
    got = call(access.chip_backup, Module(), pill())
    meta = json.load(open(os.path.join(got["path"], "meta.json")))
    meta["chip"]["uid"] = "0a0b0c0d0e0f101112131415"
    json.dump(meta, open(os.path.join(got["path"], "meta.json"), "w"))
    with pytest.raises(common.FlashError, match="read from chip 0a0b0c0d0e0f101112131415"):
        access.chip_restore(Module(), pill(), got["path"])
    assert written(pill) == []


def test_another_chip_behind_the_probe_reads_nothing(pill, monkeypatch):
    monkeypatch.setenv("CHIP_UID", "0a0b0c0d0e0f101112131415")
    with pytest.raises(common.FlashError, match="unique id"):
        access.chip_backup(Module(), pill())
    assert not os.path.exists(os.path.join(pill()["dest"], "pill")), "refused before a directory"


def test_a_write_the_chip_does_not_hold_afterwards_is_a_failure(pill, monkeypatch):
    got = call(access.chip_backup, Module(), pill())
    pill.state.write_bytes(b"\xff" * SIZE)
    monkeypatch.setenv("CHIP_BROKEN", "1")
    with pytest.raises(common.FlashError, match="does not hash to the backup"):
        access.chip_restore(Module(), pill(), got["path"])


def test_a_route_that_names_no_chip_id_touches_nothing(pill):
    with pytest.raises(common.FlashError, match="names no uid"):
        access.chip_backup(Module(), pill(route={"probe": "probe1", "chip": "STM32F103C8"}))
    assert not pill.log.exists()


def test_a_write_needs_the_go_ahead(pill):
    got = call(access.chip_backup, Module(), pill())
    pill.state.write_bytes(b"\xff" * SIZE)
    with pytest.raises(common.FlashError, match="bench_apply_allowed"):
        access.chip_restore(Module(), pill(apply_allowed=False), got["path"])
    assert written(pill) == []


def test_an_swd_board_can_be_backed_up_whole_and_by_region():
    assert common.can("swd") == ["regions", "chip"]


def erases(pill):
    return [x for x in pill.log.read_text().splitlines() if x.startswith("erase")] \
        if pill.log.exists() else []


def test_a_whole_chip_erase_needs_a_backup_of_this_chip_then_reads_erased(pill):
    before = pill.state.read_bytes()
    with pytest.raises(common.FlashError, match="no backup of this chip.*erase_without_backup"):
        access.chip_erase(Module(), pill())
    assert erases(pill) == [] and pill.state.read_bytes() == before
    got = call(access.chip_backup, Module(), pill())
    plan = call(access.chip_erase, Module(check_mode=True), pill())
    assert plan["backups"] == [os.path.basename(got["path"])] and f"uid {UID}" in plan["msg"]
    assert erases(pill) == [], "a plan erases nothing"
    done = call(access.chip_erase, Module(), pill())
    assert done["changed"] and "reads erased" in done["msg"] and pill.state.read_bytes() == b"\xff" * SIZE
    # the mass erase, with the chip's unique id in the same session
    assert erases(pill) == [f"erase --chip STM32F103C8 --probe stlink --location 1-1.3 --uid {UID}"]


def test_an_erase_the_chip_does_not_show_is_a_failure(pill, monkeypatch):
    monkeypatch.setenv("CHIP_BROKEN", "1")
    with pytest.raises(common.FlashError, match="does not read erased"):
        access.chip_erase(Module(), pill(without_backup=True))


def test_another_chip_behind_the_probe_is_not_erased(pill, monkeypatch):
    before = pill.state.read_bytes()
    monkeypatch.setenv("CHIP_UID", "0a0b0c0d0e0f101112131415")
    with pytest.raises(common.FlashError, match="unique id"):
        access.chip_erase(Module(), pill(without_backup=True))
    assert erases(pill) == [] and pill.state.read_bytes() == before


def test_another_chips_backup_does_not_count_for_an_erase(pill):
    got = call(access.chip_backup, Module(), pill())
    meta = json.load(open(os.path.join(got["path"], "meta.json")))
    meta["chip"]["uid"] = "0a0b0c0d0e0f101112131415"
    json.dump(meta, open(os.path.join(got["path"], "meta.json"), "w"))
    with pytest.raises(common.FlashError, match="no backup of this chip"):
        access.chip_erase(Module(), pill())
