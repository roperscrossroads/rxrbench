"""Timing of tasks and module phases (docs/ARCHITECTURE.md#checking-depth)."""
import importlib.util
import json
import os

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import timing

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("timing_tool", os.path.join(REPO, "tools", "timing.py"))
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def test_a_phase_is_recorded_and_one_that_raises_says_so():
    p = timing.Phases()
    with p.phase("read"):
        pass
    with pytest.raises(ValueError):
        with p.phase("write"):
            raise ValueError("no")
    assert [e["phase"] for e in p.out] == ["read", "write"]
    assert "failed" not in p.out[0] and p.out[1]["failed"] is True
    assert all(e["seconds"] >= 0 for e in p.out)


def _run(d, run, secs):
    os.makedirs(d / run)
    rec = {"v": 1, "run": run, "play": "apply", "boards": {"b1": {
        "family": "widget", "flasher": "sim", "route": None, "seconds": secs,
        "tasks": [{"task": "The declared image", "seconds": secs,
                   "phases": [{"phase": "write", "seconds": secs - 1}]}]}}}
    (d / run / "timing-apply.json").write_text(json.dumps(rec))


def test_the_report_puts_the_slowest_first_and_shows_how_a_step_moved(tmp_path):
    _run(tmp_path, "20260101T000000Z", 10.0)
    _run(tmp_path, "20260102T000000Z", 4.0)
    text = tool.report(tool.records([str(tmp_path / "20260101T000000Z"), str(tmp_path / "20260102T000000Z")]))
    lines = text.splitlines()
    assert lines[0].startswith("2 timing record(s)")
    assert "apply widget/sim: The declared image  10.0s -> 4.0s" in lines[1]
    assert "The declared image / write  9.0s -> 3.0s" in lines[2]


def test_no_records_says_how_to_get_them(tmp_path):
    assert "just recipes" in tool.report(tool.records([str(tmp_path)]))
