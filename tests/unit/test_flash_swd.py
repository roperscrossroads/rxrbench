"""The SWD route against stand-ins: a fake openocd for the host tool, and a fake host tool
for the flasher. The chip's unique id is the guard, and success is the flash's own hash."""
import hashlib
import importlib.util
import json
import os
import stat

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import layout, routes
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, swd

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UID = "0123456789abcdef01234567"


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "swd_chip", os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "bin", "swd_chip.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def script(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# ---- the host tool, against an openocd that prints what a session would ------------------

FAKE_OPENOCD = """#!/usr/bin/env python3
import os, re, sys
tcl = open(sys.argv[-1]).read()
open(os.environ["OCD_LOG"], "a").write(" ".join(sys.argv[1:-1]) + "\\n" + tcl + "\\n")
mode = os.environ.get("OCD_MODE", "ok")
print("Info : Target voltage: 3.187943", file=sys.stderr)
if mode == "silent":
    print("Error: init mode failed (unable to connect to the target)", file=sys.stderr)
    sys.exit(1)
print("Info : SWD DPIDR 0x1ba01477", file=sys.stderr)
if mode == "asleep":
    print("Error: [stm32f1x.cpu] Examination failed", file=sys.stderr)
    sys.exit(1)
uid = os.environ["OCD_UID"]
print("~uid " + uid); print("~idcode 00006410"); print("~size 131072")
if mode == "toolong":
    print("~toolong"); sys.exit(1)
if mode == "unsteady" and "set again" in tcl:
    print("~unsteady"); sys.exit(1)
m = re.search(r'\\$uidhex ne "([0-9a-f]+)"', tcl)
if m and m.group(1) != uid:
    print("~refused uid"); sys.exit(1)
m = re.search(r"dump_image (\\S+) \\S+ (\\S+)", tcl)
if m:
    n = 131072 if m.group(2) == "$size" else int(m.group(2))
    open(m.group(1), "wb").write((b"flash" * 30000)[:n])
for mark in ("verified", "erased"):
    if '"~' + mark + '"' in tcl:
        print("~" + mark)
"""


@pytest.fixture
def tool(tmp_path, monkeypatch):
    script(tmp_path / "openocd", FAKE_OPENOCD)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("OCD_UID", UID)
    monkeypatch.setenv("OCD_LOG", str(tmp_path / "ocd.log"))
    mod = load_tool()

    def call(*argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main(list(argv))
        return rc, json.loads(buf.getvalue()), (tmp_path / "ocd.log").read_text() \
            if (tmp_path / "ocd.log").exists() else ""
    return call


def test_id_says_what_the_chip_is(tool):
    rc, out, _log = tool("id", "--chip", "STM32F103C8")
    assert rc == 0
    assert out == {"uid": UID, "idcode": "0x00006410", "flash_size": 131072,
                   "dpidr": "0x1ba01477", "voltage": 3.19, "flash_base": 0x08000000}


def test_sha_is_of_the_flash_over_the_length_asked(tool):
    rc, out, _log = tool("sha", "1000", "--chip", "STM32F103C8", "--uid", UID)
    assert rc == 0 and out["length"] == 1000
    assert out["sha256"] == hashlib.sha256((b"flash" * 30000)[:1000]).hexdigest()


def test_another_chip_behind_the_probe_ends_the_session_before_the_work(tool):
    rc, out, _log = tool("sha", "1000", "--chip", "STM32F103C8", "--uid", "ff" * 12)
    assert rc == 1 and "nothing was done" in out["error"] and UID in out["error"]


@pytest.mark.parametrize("verb", ["write", "erase"])
def test_a_write_or_an_erase_without_a_unique_id_never_starts(tool, verb, tmp_path):
    img = tmp_path / "i.bin"
    img.write_bytes(b"x" * 10)
    rc, out, log = tool(verb, *([str(img)] if verb == "write" else []), "--chip", "STM32F103C8")
    assert rc == 1 and "needs --uid" in out["error"] and log == ""


def test_write_erases_programs_verifies_and_resets_in_one_guarded_session(tool, tmp_path):
    img = tmp_path / "i.bin"
    img.write_bytes(b"x" * 10)
    rc, out, log = tool("write", str(img), "--chip", "STM32F103C8", "--uid", UID)
    assert rc == 0 and out["written"] is True
    guard, write = log.index('$uidhex ne'), log.index("flash write_image erase")
    assert guard < write < log.index("verify_image") < log.index("reset run")
    assert log.count("init\n") == 1


def test_location_names_the_probe_openocd_opens(tool):
    """Two ST-Links on one host: openocd is told which, never left to take the first."""
    _rc, _out, log = tool("id", "--chip", "STM32F103C8", "--location", "1-1.2.4.3")
    head = log.splitlines()[0]
    assert "adapter usb location 1-1.2.4.3" in head
    assert head.index("interface/stlink.cfg") < head.index("adapter usb location")


@pytest.mark.parametrize("where", ["1-1;shutdown", "/dev/ttyACM0", "1-"])
def test_a_location_that_is_not_a_usb_path_is_refused(tool, where):
    rc, out, _log = tool("id", "--chip", "STM32F103C8", "--location", where)
    assert rc == 1 and "not a USB location" in out["error"]


def test_srst_connects_with_the_chip_held_in_reset(tool):
    _rc, _out, log = tool("id", "--chip", "STM32F103C8", "--srst")
    assert "connect_assert_srst" in log


@pytest.mark.parametrize("mode,says", [("asleep", "the core does not"),
                                       ("silent", "does not answer it")])
def test_a_chip_that_does_not_answer_is_named_as_such(tool, monkeypatch, mode, says):
    monkeypatch.setenv("OCD_MODE", mode)
    rc, out, _log = tool("id", "--chip", "STM32F103C8")
    assert rc == 1 and says in out["error"] and "NRST" in out["error"]
    assert out["unanswered"] is True


def test_a_failure_with_the_chip_answering_is_not_reported_as_unanswered(tool, monkeypatch):
    monkeypatch.setenv("OCD_MODE", "toolong")
    rc, out, _log = tool("sha", "0x40000", "--chip", "STM32F103C8", "--uid", UID)
    assert rc == 1 and "unanswered" not in out


def test_look_reads_the_identity_without_halting_or_resetting(tool):
    _rc, out, log = tool("id", "--chip", "STM32F103C8", "--look")
    assert out["uid"] == UID and "reset" not in log


def test_look_reads_twice_and_reports_nothing_when_the_reads_differ(tool, monkeypatch):
    """A core asleep with its debug clock off answers a running read with stale words (seen on
    an STM32F103: its unique id shifted a word): two reads that differ are no identity."""
    _rc, _out, log = tool("id", "--chip", "STM32F103C8", "--look")
    assert "set again" in log and "~unsteady" in log
    monkeypatch.setenv("OCD_MODE", "unsteady")
    rc, out, _log = tool("id", "--chip", "STM32F103C8", "--look")
    assert rc == 1 and "read differently twice" in out["error"] and "uid" not in out


@pytest.mark.parametrize("args", [("write", "x.bin", "--uid", UID), ("id", "--srst")])
def test_look_is_for_id_alone_and_never_under_reset(tool, args):
    rc, out, _log = tool(*args[:1], *args[1:], "--chip", "STM32F103C8", "--look")
    assert rc == 1 and "--look" in out["error"]


def test_an_stm32f7_is_read_where_its_reference_manual_puts_its_identity(tool):
    """RM0410: unique id at 0x1FF0F420 (96 bits), flash size in KB at 0x1FF0F442, DEV_ID in
    DBGMCU_IDCODE; its flash is the stm32f2x driver's (a mass erase by that name)."""
    _rc, out, log = tool("id", "--chip", "STM32F767ZI")
    assert "target/stm32f7x.cfg" in log and "read_memory 0x1ff0f420 32 3" in log
    assert "read_memory 0x1FF0F442 16 1" in log and out["uid"] == UID
    _rc, _out, log = tool("erase", "--chip", "STM32F767ZI", "--uid", UID)
    assert "stm32f2x mass_erase 0" in log


def test_an_unknown_chip_has_no_recipe(tool):
    rc, out, _log = tool("id", "--chip", "ATSAMD21")
    assert rc == 1 and "no SWD recipe" in out["error"]


def test_at_starts_a_hash_and_a_write_behind_the_start_of_flash(tool, tmp_path):
    _rc, _out, log = tool("sha", "1000", "--chip", "STM32F103C8", "--uid", UID, "--at", "0x08004000")
    assert "dump_image" in log and " 0x8004000 1000" in log
    assert "if {17384 > $size}" in log, "the length is counted from where it starts"
    img = tmp_path / "i.bin"
    img.write_bytes(b"x" * 10)
    rc, _out, log = tool("write", str(img), "--chip", "STM32F103C8", "--uid", UID, "--at", "0x08004000")
    assert rc == 0
    assert "flash write_image erase" in log and f"{img} 0x8004000 bin" in log
    assert f"verify_image {img} 0x8004000 bin" in log


def test_a_ranged_erase_erases_that_range_in_one_guarded_session(tool):
    rc, out, log = tool("erase", "0x8000", "--chip", "STM32F103C8", "--uid", UID, "--at", "0x08018000")
    assert rc == 0 and out["erased"] is True and out["at"] == 0x08018000 and out["length"] == 0x8000
    assert "if {131072 > $size}" in log, "a range past the end of flash stops the session"
    assert log.index("$uidhex ne") < log.index("flash erase_address 0x8018000 32768")
    assert "mass_erase" not in log


@pytest.mark.parametrize("args", [["0x8000"], ["--at", "0x08018000"]])
def test_a_ranged_erase_needs_both_where_and_how_much(tool, args):
    rc, out, log = tool("erase", *args, "--chip", "STM32F103C8", "--uid", UID)
    assert rc == 1 and "needs both --at and a length" in out["error"] and log == ""


def test_an_address_below_the_flash_or_an_image_past_its_end_writes_nothing(tool, tmp_path, monkeypatch):
    img = tmp_path / "i.bin"
    img.write_bytes(b"x" * 10)
    rc, out, log = tool("write", str(img), "--chip", "STM32F103C8", "--uid", UID, "--at", "0x1000")
    assert rc == 1 and "below the start of this chip's flash" in out["error"] and log == ""
    monkeypatch.setenv("OCD_MODE", "toolong")
    rc, out, _log = tool("write", str(img), "--chip", "STM32F103C8", "--uid", UID, "--at", "0x0801fffc")
    assert rc == 1 and "runs past the end of the chip's flash" in out["error"]


# ---- the flasher, against a host tool that holds a "flash" in a file ---------------------

FAKE_CHIP = """#!/usr/bin/env python3
import hashlib, json, os, sys
a = sys.argv[1:]
state = os.environ["CHIP_STATE"]
open(state + ".log", "a").write(" ".join(a) + "\\n")
uid = a[a.index("--uid") + 1]
if os.environ.get("CHIP_ASLEEP") == "1":
    print(json.dumps({"error": "the debug port answers (DPIDR 0x1ba01477) but the core does "
                      "not: not wired, not powered, or asleep", "unanswered": True}))
    sys.exit(1)
if uid != os.environ["CHIP_UID"]:
    print(json.dumps({"error": "the chip behind the probe has unique id X: nothing was done"}))
    sys.exit(1)
BASE = 0x08000000
off = int(a[a.index("--at") + 1], 0) - BASE if "--at" in a else 0
flash = open(state, "rb").read()
if a[0] == "write":
    data = open(a[1], "rb").read()
    if os.environ.get("CHIP_BROKEN") != "1":
        flash = flash.ljust(off, b"\\xff")
        flash = flash[:off] + data + flash[off + len(data):]
        open(state, "wb").write(flash)
    n = len(data)
elif a[0] == "read":
    n = int(a[2])
    open(a[1], "wb").write(flash[off:off + n])
else:
    n = int(a[1])
print(json.dumps({"uid": uid, "flash_size": 131072, "length": n, "flash_base": BASE,
                  "sha256": hashlib.sha256(flash[off:off + n]).hexdigest()}))
"""


@pytest.fixture
def rig(tmp_path, monkeypatch):
    img = tmp_path / "image"
    img.mkdir()
    data = b"new image " * 100
    (img / "zephyr.bin").write_bytes(data)
    manifest = {"family": "bare", "format": "raw-bin", "file": "zephyr.bin",
                "board_tag": "bluepill-f103", "hash": hashlib.sha256(data).hexdigest()}
    (img / "manifest.json").write_text(json.dumps(manifest))
    state = tmp_path / "flash.bin"
    state.write_bytes(b"\xff" * 4096)
    monkeypatch.setenv("CHIP_STATE", str(state))
    monkeypatch.setenv("CHIP_UID", UID)
    tools = {"swd_chip": script(tmp_path / "swd_chip.py", FAKE_CHIP),
             "devreg": script(tmp_path / "devreg.py",
                              "import sys\nprint('1-1.2.4.3' if '--busid' in sys.argv else '0483:3748')\n")}

    def flasher(**kw):
        m, f = common.load_image(str(img))
        args = {"probe": "probe1", "chip": "STM32F103C8", "uid": UID,
                "board_tag": "bluepill-f103", **tools, **kw}
        return swd.SwdFlasher("pill", m, f, **args), m
    flasher.state, flasher.log, flasher.data, flasher.tmp = state, tmp_path / "flash.bin.log", data, tmp_path
    return flasher


def writes(rig):
    return [x for x in rig.log.read_text().splitlines() if x.startswith("write")]


def test_a_chip_holding_other_bytes_is_written_and_then_holds_the_image(rig):
    f, m = rig()
    r = common.drive(f, m)
    assert r["changed"] and r["after"]["hash"] == m["hash"]
    assert rig.state.read_bytes().startswith(rig.data) and len(writes(rig)) == 1


def test_a_chip_already_holding_the_image_is_not_written(rig):
    f, m = rig()
    common.drive(f, m)
    r = common.drive(*rig())
    assert r["changed"] is False and len(writes(rig)) == 1


def test_a_plan_reads_and_writes_nothing(rig):
    f, m = rig()
    r = common.drive(f, m, check_mode=True)
    assert r["changed"] and writes(rig) == []


def test_a_write_the_chip_does_not_hold_afterwards_is_a_failure(rig, monkeypatch):
    monkeypatch.setenv("CHIP_BROKEN", "1")
    f, m = rig()
    with pytest.raises(common.FlashError, match="does not run the image after the flash"):
        common.drive(f, m)


def test_a_chip_whose_core_sleeps_is_unanswered_so_the_board_is_skipped(rig, monkeypatch):
    monkeypatch.setenv("CHIP_ASLEEP", "1")
    f, _m = rig()
    with pytest.raises(common.Unanswered, match="the core does not"):
        f.device()
    assert writes(rig) == []


def test_the_wrong_chip_behind_the_probe_is_refused(rig):
    f, m = rig(uid="ff" * 12)
    with pytest.raises(common.FlashError, match="nothing was done"):
        common.drive(f, m)
    assert writes(rig) == []


def test_an_image_for_another_board_is_refused_before_any_write(rig):
    f, m = rig(board_tag="heltec-v4")
    with pytest.raises(common.FlashError, match="refused: the image is for a 'bluepill-f103'"):
        common.drive(f, m)
    assert writes(rig) == []


def test_a_probe_that_is_not_an_swd_probe_is_refused(rig):
    f, m = rig(devreg=script(rig.tmp / "devreg2.py", "print('10c4:ea60')\n"))
    with pytest.raises(common.FlashError, match="is not an SWD probe|cannot resolve"):
        common.drive(f, m)


def test_every_session_names_the_probe_by_where_devreg_found_it(rig):
    f, m = rig()
    common.drive(f, m)
    lines = rig.log.read_text().splitlines()
    assert lines and all("--location 1-1.2.4.3" in line for line in lines)


@pytest.mark.parametrize("answer", [
    "import sys\nif '--busid' in sys.argv:\n    sys.exit(2)\nprint('0483:3748')\n",  # too old
    "print('0483:3748')\n",           # ignores --busid: what it prints is not a location
])
def test_a_devreg_that_cannot_say_where_the_probe_is_stops_before_openocd(rig, answer):
    f, m = rig(devreg=script(rig.tmp / "devreg-old.py", answer))
    with pytest.raises(common.FlashError, match="hosts-setup"):
        common.drive(f, m)
    assert not rig.log.exists(), "openocd was never run"


def test_the_reset_line_is_used_when_the_registry_declares_it(rig):
    f, m = rig(nrst=True)
    common.drive(f, m)
    assert all("--srst" in line for line in rig.log.read_text().splitlines())


def test_an_image_linked_behind_a_bootloader_is_hashed_and_written_at_its_base(rig):
    f, m = rig()
    f.manifest = {**m, "base": "0x8004000", "converted_from": {"format": "ihex"}}
    common.drive(f, f.manifest)
    lines = rig.log.read_text().splitlines()
    assert lines and all(line.endswith("--at 0x8004000") for line in lines), lines


def test_an_image_with_a_base_is_refused_by_a_flasher_that_writes_from_the_start():
    m = {"format": "raw-bin", "base": "0x8004000", "converted_from": {"format": "elf"}}
    assert common.common_refusals(m, ("raw-bin",)) == [
        "the image goes at 0x8004000 (staged from elf linked there); this flasher writes from "
        "the start of flash only"]
    assert common.common_refusals(m, ("raw-bin",), at_base=True) == []


def test_a_plugins_module_names_its_own_formats(tmp_path):
    (tmp_path / "img.x").write_bytes(b"x")
    (tmp_path / "manifest.json").write_text(json.dumps({"family": "bare", "format": "probe-img",
                                                         "file": "img.x"}))
    with pytest.raises(common.FlashError, match="unknown format 'probe-img'"):
        common.load_image(str(tmp_path))
    m, _f = common.load_image(str(tmp_path), formats=("probe-img",))
    assert m["format"] == "probe-img"


# ---- the registry side: a route, and a board with no console -----------------------------

ENTRY = {"kind": "swd", "policy": {"board_tag": "bluepill-f103"},
         "reach": {"swd": {"host": "benchpi1", "probe": "probe1", "chip": "STM32F103C8",
                           "uid": UID, "nrst": True, "state": "live", "can": ["flash", "diag"]}}}


def test_the_swd_route_carries_what_the_flasher_needs():
    chosen = routes.select(ENTRY)
    assert set(chosen) == {"flash", "diag"}
    assert {k: chosen["flash"][k] for k in ("bearer", "host", "probe", "chip", "uid", "nrst")} == {
        "bearer": "swd", "host": "benchpi1", "probe": "probe1", "chip": "STM32F103C8",
        "uid": UID, "nrst": True}


def test_a_chip_behind_a_probe_is_flashed_through_it_and_gets_no_broker(tmp_path):
    d = tmp_path / "lay"
    (d / "host_vars").mkdir(parents=True)
    (d / "host_vars" / "pill.yml").write_text("firmware: {family: bare, image: img1}\n")
    cat = tmp_path / "cat"
    cat.mkdir()
    (cat / "boards.yml").write_text("boards:\n  bluepill-f103: {mcu: stm32f103}\n")
    (cat / "firmware.yml").write_text(
        "images:\n  img1: {family: bare, board_tags: [bluepill-f103], flasher: swd, rf: none,\n"
        "         source: {type: path, path: /nowhere}}\n")
    reg = {"hosts": {"benchpi1": {"ssh": "bench@benchpi1"}}, "devices": {"pill": ENTRY}}
    r = layout.load_layout(str(d), {"boards": {"pill": {}}, "catalog_dir": str(cat),
                                    "location_dir": str(tmp_path / "none")}, reg=reg)
    b = r["boards"]["pill"]
    assert b["vars"]["bench_flasher"] == "swd" and b["vars"]["bench_access_host"] == "benchpi1"
    assert "no_console" in b["groups"] and b["vars"]["bench_present"] is True
    assert b["vars"]["bench_can"] == ["chip"], "a bare image declares no config range"


def test_a_chip_whose_image_declares_a_config_range_has_the_region_level(tmp_path):
    """docs/ARCHITECTURE.md#chip-and-region-flows: the ranges declared with role config are the
    config regions, so an SWD board whose image declares one can have them backed up."""
    d = tmp_path / "lay"
    (d / "host_vars").mkdir(parents=True)
    (d / "host_vars" / "pill.yml").write_text("firmware: {family: bare, image: img1}\n")
    cat = tmp_path / "cat"
    cat.mkdir()
    (cat / "boards.yml").write_text("boards:\n  bluepill-f103: {mcu: stm32f103}\n")
    (cat / "firmware.yml").write_text(
        "images:\n  img1: {family: bare, board_tags: [bluepill-f103], flasher: swd, rf: none,\n"
        "         source: {type: path, path: /nowhere},\n"
        "         regions: [{label: app, kind: app, start: '0x08000000', size: '0xF000'},\n"
        "                   {label: config, kind: data, start: '0x0800F000', size: '0x1000', role: config}]}\n")
    reg = {"hosts": {"benchpi1": {"ssh": "bench@benchpi1"}}, "devices": {"pill": ENTRY}}
    r = layout.load_layout(str(d), {"boards": {"pill": {}}, "catalog_dir": str(cat),
                                    "location_dir": str(tmp_path / "none")}, reg=reg)
    b = r["boards"]["pill"]
    assert not r.get("problems"), r.get("problems")
    assert b["vars"]["bench_can"] == ["regions", "chip"]
    assert [x["label"] for x in b["vars"]["bench_flash_regions"] if x.get("role") == "config"] == ["config"]


# ---- the write rule over declared ranges (docs/ARCHITECTURE.md#partitions-and-the-write-rule) ----

RANGES = [{"start": 0x08000000, "size": 0x1000, "label": "bootloader", "kind": "boot"},
          {"start": 0x08001000, "size": 0x7000, "label": "app", "kind": "app"}]


def test_a_write_over_the_declared_bootloader_is_refused_unless_named(rig):
    rig.state.write_bytes(b"\xff" * 0x8000)
    f, m = rig()
    f.flash_regions = RANGES
    with pytest.raises(common.FlashError, match="covers bootloader at 0x8000000"):
        common.drive(f, m)
    assert writes(rig) == []
    f, m = rig()
    f.flash_regions, f.overwrite = RANGES, ("bootloader",)
    assert common.drive(f, m)["changed"] and rig.state.read_bytes().startswith(rig.data)


def test_an_image_linked_into_the_app_range_is_written_after_its_start_is_read(rig):
    rig.state.write_bytes(b"\xff" * 0x8000)
    f, m = rig()
    f.manifest = {**m, "base": "0x8001000", "converted_from": {"format": "ihex"}}
    f.flash_regions = RANGES
    assert common.drive(f, f.manifest)["changed"]
    lines = rig.log.read_text().splitlines()
    assert any(x.startswith("read ") and x.endswith("--at 134221824") for x in lines), lines
    assert rig.state.read_bytes()[0x1000:0x1000 + len(rig.data)] == rig.data


def _probe_board(tmp_path, entry, host_vars, image_rows):
    d = tmp_path / "lay"
    (d / "host_vars").mkdir(parents=True, exist_ok=True)
    (d / "host_vars" / "nf7.yml").write_text(host_vars)
    cat = tmp_path / "cat"
    cat.mkdir(exist_ok=True)
    (cat / "boards.yml").write_text("boards:\n  nucleo-f767zi: {mcu: stm32f767}\n")
    (cat / "firmware.yml").write_text("images:\n" + image_rows)
    reg = {"hosts": {"benchpi1": {"ssh": "bench@benchpi1"}}, "devices": {"nf7": entry}}
    return layout.load_layout(str(d), {"boards": {"nf7": {}}, "catalog_dir": str(cat),
                                       "location_dir": str(tmp_path / "none")}, reg=reg)


NF7 = {"kind": "swd", "firmware": "port", "policy": {"board_tag": "nucleo-f767zi"},
       # its console is USART3 through the probe's own serial port
       "match": {"serial_by_id": "usb-STMicroelectronics_STM32_STLink_0671FF48-if02"},
       "reach": {"usb": {"host": "benchpi1", "can": ["shell", "diag"]},
                 "swd": {"host": "benchpi1", "probe": "probe1", "chip": "STM32F767ZI",
                         "uid": "003a", "state": "live", "can": ["flash", "diag"]},
                 "eth": {"host": "benchpi1", "ip": "192.0.2.190", "state": "live", "can": ["flash"]}}}
SMP_IMG = ("  smp1: {family: zephyr_mcumgr, board_tags: [nucleo-f767zi], flasher: smp, rf: none,\n"
           "         source: {type: path, path: /nowhere}}\n")


def test_a_probe_boards_console_on_the_probes_serial_port_gets_a_broker(tmp_path):
    """A Nucleo's ST-Link carries USART3: the board's entry matches that port and has a usb
    route for its shell beside its swd route; a Zephyr application with MCUmgr says it has a
    shell (bench_console), and is updated over the network route the layout pins."""
    r = _probe_board(tmp_path, NF7, "firmware: {family: zephyr_mcumgr, image: smp1}\n"
                     "bench_console: zephyr-shell\nroutes: {flash: eth}\n", SMP_IMG)
    b = r["boards"]["nf7"]
    assert b["vars"]["bench_via"] == "console" and "no_console" not in b["groups"]
    assert b["vars"]["bench_routes"]["shell"]["bearer"] == "usb"
    assert b["vars"]["bench_routes"]["flash"]["bearer"] == "eth"
    assert b["vars"]["bench_routes"]["flash"]["ip"] == "192.0.2.190"
    assert b["vars"]["bench_flasher"] == "smp"


def test_an_mcumgr_application_without_a_declared_shell_has_no_console(tmp_path):
    r = _probe_board(tmp_path, NF7, "firmware: {family: zephyr_mcumgr, image: smp1}\n"
                     "routes: {flash: eth}\n", SMP_IMG)
    assert "no_console" in r["boards"]["nf7"]["groups"]


def test_a_console_a_zephyr_application_cannot_have_is_refused():
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families
    fam = families.plugin("zephyr_mcumgr")
    hv = {"nf7": {"firmware": {"family": "zephyr_mcumgr"}, "bench_console": "nsh"}}
    assert any("bench_console 'nsh'" in p for p in fam.check_layout(hv))
    assert fam.check_layout({"nf7": {"firmware": {"family": "zephyr_mcumgr"},
                                     "bench_console": "zephyr-shell"}}) == []
