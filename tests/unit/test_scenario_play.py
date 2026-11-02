"""`just scenario` end to end: the play, a real broker, a fake shell behind it. Steps are judged
in order, a step may take `within` seconds to come true, a failed step ends its scenario, and
what each step said is kept under runs/. A step named in words (spaces) runs; a layout that
declares no scenario is refused, not reported as every scenario passing."""
import importlib.util
import json
import os
import subprocess
import threading
import time

import pytest
import yaml

from tests.support.fake_shell import FakeShell

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAY = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "playbooks",
                    "scenario.yml")
spec = importlib.util.spec_from_file_location("broker_for_scenarios", os.path.join(
    REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "broker.py"))
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)


@pytest.fixture
def bench(tmp_path):
    """A board `n1` behind a real broker; its shell learns a peer after the third ask."""
    asked = {"n": 0}

    def nodedb(_shell):
        asked["n"] += 1
        return "nodes: 2\n0x0a0b0c0d\n0x01020304" if asked["n"] >= 3 else "nodes: 1\n0x0a0b0c0d"

    shell = FakeShell({"node ask 0x01020304": "asked", "node list": nodedb}, ring=4096)
    console = mb.Console("n1", mb.path_opener(shell.path), str(tmp_path / "n1.log"))
    broker = mb.Broker(console, str(tmp_path / "run" / "n1.sock"))
    t = threading.Thread(target=broker.run, daemon=True)
    t.start()
    end = time.time() + 3
    while not console.is_open() and time.time() < end:
        time.sleep(0.01)
    yield tmp_path, broker.sock_path, asked
    broker.shutdown()
    t.join(timeout=2)
    shell.close()


def run(tmp_path, sock, scenarios, extra=None):
    inv = {"boards": {"hosts": {"n1": {"bench_board": "n1", "bench_via": "console",
                                       "bench_family": "fakeos", "bench_socket": sock}},
                      "vars": {"ansible_connection": "local",
                               "ansible_python_interpreter": os.path.join(REPO, "venv", "bin", "python"),
                               "bench_layout": "t", "bench_repo": str(tmp_path), "bench_runs_dir": str(tmp_path / "runs"),
                               **({"bench_scenarios": scenarios} if scenarios is not None else {}),
                               **(extra or {})}}}
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump(inv))
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=os.path.join(REPO, "collections"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0")
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-playbook"), "-i",
                        str(tmp_path / "hosts.yml"), PLAY], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout + r.stderr


def record(tmp_path, name):
    [path] = [os.path.join(d, "scenario.json") for d, _s, files in os.walk(tmp_path / "runs")
              if "scenario.json" in files and os.path.basename(d) == name]
    with open(path) as f:
        return json.load(f), os.path.dirname(path)


def test_steps_named_in_words_run_in_order_and_one_may_take_its_time(bench):
    tmp, sock, asked = bench
    rc, out = run(tmp, sock, [{"name": "hears", "steps": [
        {"name": "ask the peer", "board": "n1", "cmd": "node ask 0x01020304", "expect": "asked"},
        {"name": "the peer in the node DB", "board": "n1", "cmd": "node list",
         "expect": "0x01020304", "within": 10}]}])
    assert rc == 0, out
    rec, d = record(tmp, "hears")
    assert rec["passed"] and [s["passed"] for s in rec["steps"]] == [True, True]
    assert sorted(os.listdir(d)) == ["01-ask-the-peer.txt", "02-the-peer-in-the-node-DB.txt",
                                     "scenario.json"]
    assert asked["n"] >= 3, "the second step was asked again until it came true"


def test_a_failed_step_ends_its_scenario_and_fails_the_play(bench):
    tmp, sock, _asked = bench
    rc, out = run(tmp, sock, [{"name": "never", "steps": [
        {"board": "n1", "cmd": "node list", "expect": "0xdeadbeef"},
        {"board": "n1", "cmd": "node ask 0x01020304", "expect": "asked"}]}])
    assert rc != 0 and "failed: never" in out, out
    rec, _d = record(tmp, "never")
    assert not rec["passed"] and len(rec["steps"]) == 1, "the step after the failure did not run"


def test_a_layout_with_no_scenarios_is_refused_not_passed(bench):
    tmp, sock, _asked = bench
    rc, out = run(tmp, sock, None)
    assert rc != 0 and "declares no bench_scenarios" in out, out


def library(tmp_path, test):
    """A site with one library test, `peer`, and a layout `t` in it."""
    (tmp_path / "site" / "tests").mkdir(parents=True)
    (tmp_path / "site" / "layouts" / "t").mkdir(parents=True)
    (tmp_path / "site" / "tests" / "peer.yml").write_text(yaml.safe_dump(test))
    return str(tmp_path / "site" / "layouts" / "t")


PEER = {"needs": {"roles": {"a": {"families": ["fakeos"]}}},
        "steps": [{"name": "ask", "board": "$a", "cmd": "node ask 0x01020304", "expect": "asked"}]}


def test_a_bound_library_test_runs_as_a_scenario_of_its_name_and_records_its_binding(bench):
    tmp, sock, _asked = bench
    layout = library(tmp, PEER)
    rc, out = run(tmp, sock, None, {"bench_layout_dir": layout,
                                    "bench_test_bindings": {"peer": {"a": "n1"}}})
    assert rc == 0, out
    rec, _d = record(tmp, "peer")
    assert rec["passed"] and rec["test"] == "peer" and rec["bindings"] == {"a": "n1"}


def test_a_role_left_unbound_refuses_the_run_and_names_the_boards_that_fit(bench):
    tmp, sock, _asked = bench
    layout = library(tmp, PEER)
    rc, out = run(tmp, sock, [{"name": "inline", "steps": [
        {"board": "n1", "cmd": "node ask 0x01020304", "expect": "asked"}]}],
        {"bench_layout_dir": layout, "bench_test_bindings": {"peer": {}}})
    assert rc != 0 and "no board is bound to role a; boards that fit it: n1" in out, out
    assert not (tmp / "runs").exists(), "nothing ran, not even the inline scenario"
