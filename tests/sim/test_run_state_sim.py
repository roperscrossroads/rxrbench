"""`just run` (tools/run.py) on the simulated Zephyr bench: one run name for a plan that records
the bench's state (the `rxrbench.core.state` callback) and the tests and scenarios after it
(docs/ARCHITECTURE.md#runs-tests-and-campaigns)."""
import json
import os
import subprocess
import sys

from tests.sim.test_zephyr_sim import AS_B, REPO, VENV, bench, play, pytestmark, recap  # noqa: F401  pylint: disable=unused-import


def run(sim_run, runs_dir):
    env = {**os.environ, "RXRBENCH_SIM_RUN": sim_run, "ANSIBLE_FORCE_COLOR": "0",
           "RXRBENCH_SITE": "site.example", "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "run.py"), "zsim",
                        "-e", json.dumps({"bench_runs_dir": runs_dir})],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=900, check=False)
    out = r.stdout + r.stderr
    name = next(line.split()[1].rstrip(":") for line in out.splitlines() if line.startswith("run "))
    with open(os.path.join(runs_dir, name, "state.json"), encoding="utf-8") as fh:
        return r.returncode, out, name, json.load(fh)


def test_a_run_records_the_state_its_tests_ran_against(bench, tmp_path):
    runs_dir = str(tmp_path / "runs")
    rc, out = play(bench, "apply.yml", "--limit", "zb3,zb4,nb1")
    assert rc == 0, out
    rc, out, name, state = run(bench, runs_dir)
    assert rc == 0, out
    assert state["run"] == name and state["layout"] == "zsim" and state["clean"] is True, state
    zb1 = state["boards"]["zb1"]
    assert zb1["changes"] == 0 and zb1["answered"] and zb1["declared"]["image"] == "zephyr-shell"
    assert zb1["reported"] != "not reported", zb1
    # the chip findings of the plan (the sim's facts record no chip: none) and every route's
    assert all(isinstance(b["foreign"], list) for b in state["boards"].values()), state["boards"]
    assert state["tool"]["commit"], state
    # the tests and scenarios of the run sit beside its state
    assert os.path.isdir(os.path.join(runs_dir, name, "zb1", "test")), os.listdir(os.path.join(runs_dir, name))
    assert os.path.isdir(os.path.join(runs_dir, name, "scenario")), os.listdir(os.path.join(runs_dir, name))
    # and how long each play, task and flasher phase took (docs/ARCHITECTURE.md#checking-depth)
    for play_name in ("plan", "test", "scenario"):
        assert os.path.isfile(os.path.join(runs_dir, name, f"timing-{play_name}.json")), play_name
    with open(os.path.join(runs_dir, name, "timing-plan.json"), encoding="utf-8") as fh:
        timed = json.load(fh)["boards"]["zb1"]
    assert timed["family"] == "bare" and timed["flasher"] == "sim", timed
    phases = [p["phase"] for t in timed["tasks"] for p in t.get("phases", [])]
    assert "read" in phases and "preflight" not in phases, phases   # a plan of 0 stops at the read

    # A board that runs something else than declared: the run still tests, and says the state
    # was not clean.
    rc, out = play(bench, "apply.yml", *AS_B)
    assert rc == 0, out
    rc, out, name, state = run(bench, runs_dir)
    assert state["clean"] is False and state["boards"]["zb1"]["changes"] == 1, state
    assert "NOT clean" in out, out
    with open(os.path.join(runs_dir, name, "timing-plan.json"), encoding="utf-8") as fh:
        timed = json.load(fh)["boards"]["zb1"]
    phases = [p["phase"] for t in timed["tasks"] for p in t.get("phases", [])]
    assert "read" in phases and "preflight" in phases, phases       # a plan of 1 goes on to it
