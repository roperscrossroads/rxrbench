"""`just test` on a board with no console and no ssh (core-portability P6): its device
tests run through its family's step tasks, one test each, and are recorded like any other."""
import json
import os
import subprocess

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAY = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "playbooks", "test.yml")
FIXTURES = os.path.join(REPO, "tests", "fixtures", "collections")


def run(tmp_path, tests, step_tasks="step_widget"):
    board = {"bench_board": "w1", "bench_via": None, "bench_family": "widget",
             "bench_plugin": "fixture.widgets", "bench_tests": tests,
             **({"bench_step_tasks": step_tasks} if step_tasks else {})}
    inv = {"boards": {"hosts": {"w1": board},
                      "vars": {"ansible_connection": "local", "bench_layout": "t",
                               "ansible_python_interpreter": os.path.join(REPO, "venv", "bin", "python"),
                               "bench_repo": str(tmp_path), "bench_runs_dir": str(tmp_path / "runs")}}}
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump(inv))
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=os.pathsep.join([os.path.join(REPO, "collections"), FIXTURES]),
               ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0")
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-playbook"), "-i", str(tmp_path / "hosts.yml"),
                        PLAY], cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout + r.stderr


def record(tmp_path):
    [p] = [os.path.join(d, "test.json") for d, _s, f in os.walk(tmp_path / "runs") if "test.json" in f]
    return json.load(open(p))


def test_a_board_with_no_console_is_tested_through_its_familys_step_tasks(tmp_path):
    rc, out = run(tmp_path, [{"name": "version", "cmd": "widget version", "expect": "^widget 1"},
                             {"name": "state", "cmd": "widget state", "expect": "^~S"}])
    assert rc == 0, out
    rec = record(tmp_path)
    assert [(r["name"], r["passed"]) for r in rec["results"]] == [("version", True), ("state", True)]


def test_a_failed_test_through_steps_fails_the_play_and_is_kept(tmp_path):
    rc, out = run(tmp_path, [{"name": "version", "cmd": "widget version", "expect": "^widget 9"},
                             {"name": "nothing", "cmd": "widget unknown", "expect": "."}])
    assert rc != 0 and "failed version, nothing" in " ".join(out.split()), out
    assert [r["passed"] for r in record(tmp_path)["results"]] == [False, False]


def test_a_board_with_no_route_and_no_step_tasks_is_refused(tmp_path):
    rc, out = run(tmp_path, [{"name": "v", "cmd": "widget version"}], step_tasks=None)
    assert rc != 0 and "no console, no ssh route and no family step tasks" in " ".join(out.split()), out
