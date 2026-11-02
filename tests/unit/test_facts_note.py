"""tools/facts_note.py: a probe's result in a device's facts, written only when it is new."""
import json
import os
import subprocess
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(REPO, "tools", "facts_note.py")


def note(facts, result):
    r = subprocess.run([sys.executable, TOOL, "--facts-dir", str(facts), "--key", "board1",
                        "--probe", "bootloader"], input=json.dumps(result), capture_output=True,
                       text=True, check=True)
    return r.stdout


def test_a_result_already_held_changes_no_file(tmp_path):
    """just boot on a board whose bootloader was read before: nothing new, nothing to commit
    (each run used to stamp a new time, and the site's facts commit said changed)."""
    assert "recorded" in note(tmp_path, {"version": "0.9.2-OTAFIX2.5"})
    f = tmp_path / "board1.yaml"
    before = (f.read_text(), os.stat(f).st_mtime_ns)
    assert "unchanged" in note(tmp_path, {"version": "0.9.2-OTAFIX2.5"})
    assert (f.read_text(), os.stat(f).st_mtime_ns) == before
    assert "recorded" in note(tmp_path, {"version": "0.9.2-OTAFIX2.5-zt1"})
    assert yaml.safe_load(f.read_text())["probes"]["bootloader"]["version"] == "0.9.2-OTAFIX2.5-zt1"


def test_a_bootloader_read_a_later_write_made_stale_is_forgotten(tmp_path):
    """A whole-chip write forgets the bootloader read, so `just boot` does not plan from a read
    older than the write that replaced the bootloader."""
    note(tmp_path, {"kind": "mcuboot", "key": "abc"})
    forget = [sys.executable, TOOL, "--facts-dir", str(tmp_path), "--key", "board1",
              "--probe", "bootloader", "--forget"]
    assert "forgotten" in subprocess.run(forget, capture_output=True, text=True, check=True).stdout
    assert "bootloader" not in (yaml.safe_load((tmp_path / "board1.yaml").read_text()).get("probes") or {})
    assert "no probes.bootloader" in subprocess.run(forget, capture_output=True, text=True,
                                                    check=True).stdout
