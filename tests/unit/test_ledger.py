"""tools/ledger.py and the exercise's --stale-only: each step is fingerprinted by the code it depends
on and the board's own inputs; a step that passed against the same fingerprint, board type and
firmware is fresh and left out; a change to its code, another firmware, a failure or an unknown
firmware makes it run again; a chosen verb brings the backup it needs."""
import importlib.util
import os
import sys

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(REPO, "tools"))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, "tools", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ledger = _load("ledger")
features = sys.modules.get("features") or _load("features")
from tests.unit import test_exercise as tx  # noqa: E402  (the fake plays and fixture boards)

pytestmark = pytest.mark.plugins     # step features come from every bundled collection's index

MESH = {"bench_family": "zephyrtastic", "bench_flasher": "esptool", "bench_board_tag": "heltec-v4",
        "bench_routes": {"shell": {"bearer": "usb"}}, "firmware": {"family": "zephyrtastic", "image": "zt-1"}}
LAUNCHER = {"bench_family": "launcher", "bench_flasher": "esptool", "bench_board_tag": "cyd",
            "bench_routes": {"shell": {"bearer": "usb"}}, "firmware": {"family": "launcher", "version": "3.0.0"}}


def test_the_firmware_identity_names_what_tells_images_apart_or_is_unknown():
    assert ledger.fw_id(MESH) == "family=zephyrtastic;image=zt-1"
    assert ledger.fw_id({"bench_family": "bare", "firmware": {"family": "bare"}}) == ledger.UNKNOWN
    assert ledger.fw_id({}) == ledger.UNKNOWN


def test_a_step_depends_on_its_verb_family_flasher_routes_and_its_plugins_capabilities():
    feats = features.load()
    mesh = ledger.step_features(MESH, "apply", feats)
    assert {"core.verb.apply", "mesh.family.zephyrtastic", "core.flasher.esptool", "core.route.usb"} <= set(mesh)
    assert not [f for f in mesh if f.startswith("launcher.")]
    launcher = ledger.step_features(LAUNCHER, "apply", feats)
    assert {"launcher.family.launcher", "launcher.self-update"} <= set(launcher)
    assert not [f for f in launcher if f.startswith("mesh.")]


def test_a_launcher_change_leaves_a_mesh_boards_fingerprint_alone(monkeypatch):
    feats = features.load()
    mesh_ids = ledger.step_features(MESH, "apply", feats)
    launcher_ids = ledger.step_features(LAUNCHER, "apply", feats)
    before = (ledger.fingerprint(MESH, mesh_ids, feats), ledger.fingerprint(LAUNCHER, launcher_ids, feats))
    real = ledger._hash_files                                  # pylint: disable=protected-access

    def edited(h, paths):                                      # as if launcher_serial.py had changed
        real(h, paths)
        if any(p.endswith("launcher_serial.py") for p in paths):
            h.update(b"an edit")
    monkeypatch.setattr(ledger, "_hash_files", edited)
    after = (ledger.fingerprint(MESH, mesh_ids, feats), ledger.fingerprint(LAUNCHER, launcher_ids, feats))
    assert after[0] == before[0] and after[1] != before[1]


def test_the_boards_own_inputs_are_in_its_fingerprint():
    feats = features.load()
    ids = ledger.step_features(MESH, "apply", feats)
    other = {**MESH, "firmware": {"family": "zephyrtastic", "image": "zt-2"}}
    assert ledger.fingerprint(MESH, ids, feats) == ledger.fingerprint(dict(MESH), ids, feats)
    assert ledger.fingerprint(MESH, ids, feats) != ledger.fingerprint(other, ids, feats)


def entry(**kw):
    return {"ts": "20261114T120000Z", "board": "b1", "verb": "apply", "board_tag": "t", "fw_id": "f",
            "fp": "x", "result": "pass", "tool_commit": "abc", **kw}


@pytest.mark.parametrize("last,args,want", [
    (entry(), ("t", "f", "x"), True),
    (entry(fp="y"), ("t", "f", "x"), False),               # its code changed
    (entry(result="fail"), ("t", "f", "x"), False),
    (entry(fw_id="g"), ("t", "f", "x"), False),            # other firmware
    (entry(board_tag="u"), ("t", "f", "x"), False),
    (entry(), ("t", ledger.UNKNOWN, "x"), False),          # unknown firmware is never fresh
])
def test_fresh_is_a_pass_against_the_same_code_board_type_and_firmware(last, args, want):
    ok, _ = ledger.fresh([entry(result="fail", fp="old"), last], "b1", "apply", *args)
    assert ok is want


def test_an_old_pass_is_stale_past_the_age_limit():
    import datetime
    now = datetime.datetime(2026, 12, 1, tzinfo=datetime.timezone.utc)
    assert ledger.fresh([entry()], "b1", "apply", "t", "f", "x", max_age_days=30, now=now)[0] is True
    assert ledger.fresh([entry()], "b1", "apply", "t", "f", "x", max_age_days=7, now=now)[0] is False


def run_twice(tmp_path, second_entries_edit=None, destructive=False):
    (tmp_path / "lay").mkdir(exist_ok=True)
    first = tx.make(tmp_path, tx.Plays(), destructive=destructive)
    first.v_update_there_and_back = lambda: (tx.exercise.PASS, {}, "")
    rec = first.go()
    assert rec["v"] == 2 and rec["board_tag"] == "type-a" and rec["firmware"]["id"] == "family=fam;image=fam-a"
    lines = ledger.lines_of(rec)
    if second_entries_edit:
        lines = second_entries_edit(lines)
    plays = tx.Plays()
    second = tx.exercise.Exercise("lay", "b1", destructive=destructive, runs=str(tmp_path), now="T2", plays=plays,
                                  hv=tx.FULL, images=tx.IMAGES, layout_dir=str(tmp_path / "lay"),
                                  hosts_dir=str(tmp_path / "hosts"), alts=(), stale_only=True, entries=lines)
    second.v_update_there_and_back = lambda: (tx.exercise.PASS, {}, "")
    return second.go(), plays.names()


def test_a_second_run_with_nothing_changed_leaves_every_fresh_verb_out(tmp_path):
    rec, names = run_twice(tmp_path)
    assert names[0] == "survey" or "plan" in names
    assert "apply" not in names and "snapshot" not in names and "backup" not in names
    assert "plan" in names and names[-1] == "plan-end", "the start plan and came-back always run"
    steps = {s["verb"]: s for s in rec["steps"]}
    assert steps["apply"]["result"] == tx.exercise.SKIP and steps["apply"]["msg"].startswith("fresh:")
    assert rec["selected"]["stale_only"] and "apply" in rec["selected"]["left_out"]


def test_a_verb_that_failed_last_time_runs_again_with_the_backup_it_needs(tmp_path):
    def erase_failed(lines):
        return [{**ln, "result": "fail"} if ln["verb"] == "erase-chip" else ln for ln in lines]
    _rec, names = run_twice(tmp_path, erase_failed, destructive=True)
    assert "erase-chip" in names and "backup" in names, names
    assert "apply" not in names
