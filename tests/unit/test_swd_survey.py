"""The survey's tool for a debug probe (host/bin/swd_survey.py, family bare's): each chip the
registry wires to the probe, read through it without halting it, its uid set beside the
registry's; the probe named by its USB location."""
import importlib.util
import json
import os
import stat

from ansible_collections.rxrbench.core.plugins.plugin_utils import families

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "bin")

DEVREG = """import sys
if "--busid" in sys.argv:
    print("1-1.2.4")
else:
    print("0483:374b")
"""
SWD_CHIP = """import json, os, sys
a = sys.argv[1:]
open(os.environ["SWD_LOG"], "a").write(" ".join(a) + "\\n")
chip = a[a.index("--chip") + 1]
if chip == "STM32F999":
    print(json.dumps({"error": "no SWD recipe for chip 'STM32F999'"})); sys.exit(1)
print(json.dumps({"uid": "003a001a3034511930363235", "idcode": "0x10016451",
                  "flash_size": 2097152, "voltage": 3.24, "dpidr": "0x5ba02477"}))
"""


def load():
    spec = importlib.util.spec_from_file_location("swd_survey", os.path.join(BIN, "swd_survey.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(tmp_path, monkeypatch, capsys, wired):
    for name, body in (("devreg.py", DEVREG), ("swd_chip.py", SWD_CHIP)):
        (tmp_path / name).write_text(body)
        (tmp_path / name).chmod(0o755 | stat.S_IEXEC)
    monkeypatch.setenv("SWD_LOG", str(tmp_path / "log"))
    rc = load().main(["--probe", "stlink-nucleof7", "--wired", json.dumps(wired),
                      "--devreg", str(tmp_path / "devreg.py"),
                      "--swd-chip", str(tmp_path / "swd_chip.py")])
    log = (tmp_path / "log").read_text() if (tmp_path / "log").exists() else ""
    return rc, json.loads(capsys.readouterr().out), log


def test_each_wired_chip_is_read_by_look_through_the_probe_at_its_location(tmp_path, monkeypatch, capsys):
    rc, out, log = run(tmp_path, monkeypatch, capsys, [
        {"name": "nucleof7", "chip": "STM32F767ZI", "uid": "003A001A3034511930363235"},
        {"name": "other", "chip": "STM32F767ZI", "uid": "ffff"},
        {"name": "fresh", "chip": "STM32F767ZI", "uid": None}])
    assert rc == 0 and out["location"] == "1-1.2.4"
    assert all("--look" in ln and "--location 1-1.2.4" in ln and "--probe stlink" in ln
               and "--uid" not in ln for ln in log.splitlines())
    by = {c["name"]: c for c in out["chips"]}
    assert by["nucleof7"]["uid_matches"] is True and by["nucleof7"]["flash_size"] == 2097152
    assert by["other"]["uid_matches"] is False
    assert by["fresh"]["uid_matches"] is None and by["fresh"]["registry_uid"] is None


def test_a_chip_under_reset_is_skipped_and_an_unreadable_one_says_why(tmp_path, monkeypatch, capsys):
    _rc, out, log = run(tmp_path, monkeypatch, capsys, [
        {"name": "pill2", "chip": "STM32F103C8", "uid": "0237", "nrst": True},
        {"name": "odd", "chip": "STM32F999", "uid": "1"}])
    by = {c["name"]: c for c in out["chips"]}
    assert "under reset" in by["pill2"]["skipped"] and "pill2" not in log
    assert "no SWD recipe" in by["odd"]["error"]


def test_bare_is_the_family_whose_tool_reads_a_debug_probe():
    spec = families.specs()["bare"]["survey"]
    assert spec == {"tool": "swd_survey.py", "probes": ["swd-probe"]}
    assert os.path.isfile(os.path.join(BIN, spec["tool"]))
    fam = families.plugin("bare")
    probe = {"chips": [{"name": "nucleof7", "chip": "STM32F767ZI", "uid": "003a",
                        "idcode": "0x10016451", "flash_size": 2097152, "registry_uid": None,
                        "uid_matches": None}]}
    assert "no uid in the registry yet" in fam.survey_describe(probe)
    assert fam.survey_tags({"chips": [{"uid_matches": False}]}) == {"swd:uid-differs"}
