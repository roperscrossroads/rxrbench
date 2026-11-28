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

    def failing(_shell):
        asked["fail"] = asked.get("fail", 0) + 1
        return "failed"

    def health(_shell):
        asked["up"] = asked.get("up", 10) + 1
        return 'counters\n{"up": %d, "fail": %d, "tx": %d}' % (asked["up"], asked.get("fail", 0),
                                                            asked["up"])

    def job(sh):
        # a command that returns at once; its result is printed later, among the log lines
        asked["job"] = asked.get("job", 0) + 1
        threading.Timer(0.4, lambda: sh.out("\r\njob: 3 parts\r\njob done: rc 0, 12 ms, peak 900 B\r\n"
                                             + "\x1b[1;32muart:~$ \x1b[m")).start()
        return "job started"

    def part(n):
        def handler(_sh):
            asked.setdefault("parts", []).append(n)
            return f"part {n}/3"
        return handler

    shell = FakeShell({"node ask 0x01020304": "asked", "node list": nodedb, "node fail": failing,
                       "health": health, "job start": job, "part 1": part(1), "part 2": part(2),
                       "part 3": part(3)}, ring=4096)
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


HEALTH = {"cmd": "health", "extract": r"(\{.*\})", "restart": "up", "ignore": ["tx"]}


def test_a_step_judges_how_counters_moved_since_a_kept_reading(bench):
    tmp, sock, _asked = bench
    rc, out = run(tmp, sock, [{"name": "clean", "steps": [
        {"board": "n1", "cmd": "health", "keep": "h"},
        {"board": "n1", "cmd": "node ask 0x01020304", "expect": "asked"},
        {"board": "n1", "cmd": "health", "delta": {**HEALTH, "against": "h"}}]}])
    assert rc == 0, out
    rec, _d = record(tmp, "clean")
    assert rec["passed"] and rec["steps"][2]["deltas"] == {"up": 1, "fail": 0, "tx": 1}


def test_a_counter_that_grew_fails_the_step_with_the_reason(bench):
    tmp, sock, _asked = bench
    rc, out = run(tmp, sock, [{"name": "dirty", "steps": [
        {"board": "n1", "cmd": "health", "keep": "h"},
        {"board": "n1", "cmd": "node fail"},
        {"board": "n1", "cmd": "health", "delta": {**HEALTH, "against": "h"}}]}])
    assert rc != 0, out
    rec, _d = record(tmp, "dirty")
    assert not rec["passed"] and rec["steps"][2]["why"] == ["fail grew by 1 (0 -> 1)"]


def test_counters_true_reads_each_board_with_counters_before_and_after(bench):
    tmp, sock, _asked = bench
    rc, out = run(tmp, sock, [
        {"name": "fine", "counters": True, "steps": [
            {"board": "n1", "cmd": "node ask 0x01020304", "expect": "asked"}]},
        {"name": "sends fail", "counters": True, "steps": [
            {"board": "n1", "cmd": "node fail", "expect": "failed"}]},
        {"name": "not asked", "steps": [
            {"board": "n1", "cmd": "node fail", "expect": "failed"}]}],
        {"bench_health_counters": HEALTH})
    assert rc != 0 and "failed: sends fail" in out, out
    rec, _d = record(tmp, "fine")
    assert rec["passed"] and [s["name"] for s in rec["steps"]] == [
        "01-n1-counters-before", "02-n1", "03-n1-counters-after"]
    rec, _d = record(tmp, "sends fail")
    assert not rec["passed"] and rec["steps"][-1]["why"] == ["fail grew by 1 (0 -> 1)"]
    rec, _d = record(tmp, "not asked")
    assert rec["passed"] and len(rec["steps"]) == 1


def test_the_reading_after_runs_and_is_kept_when_a_step_failed(bench):
    tmp, sock, _asked = bench
    rc, out = run(tmp, sock, [{"name": "broke", "counters": True, "steps": [
        {"board": "n1", "cmd": "node fail", "expect": "failed"},
        {"board": "n1", "cmd": "node list", "expect": "0xdeadbeef"},
        {"board": "n1", "cmd": "node ask 0x01020304", "expect": "asked"}]}],
        {"bench_health_counters": HEALTH, "bench_counters_allow": {"fail": 1}})
    assert rc != 0, out
    rec, _d = record(tmp, "broke")
    names = [s["name"] for s in rec["steps"]]
    assert names == ["01-n1-counters-before", "02-n1", "03-n1", "05-n1-counters-after"], \
        "the step after the failure did not run; the reading after did"
    assert not rec["passed"]
    assert rec["steps"][-1]["passed"] and rec["steps"][-1]["deltas"]["fail"] == 1, \
        "the board's allowance let one failure through, and the change is kept"


JOB = r"^job done: rc (?P<rc>-?\d+), (?P<ms>\d+) ms, peak (?P<peak>\d+) B"


def test_a_step_awaits_the_line_its_command_prints_later_and_judges_its_numbers(bench):
    tmp, sock, asked = bench
    rc, out = run(tmp, sock, [{"name": "job", "steps": [
        {"name": "run the job", "board": "n1", "cmd": "job start", "refuse": "error",
         "await": {"pattern": JOB, "within": 10},
         "limits": {"rc": {"eq": 0}, "ms": {"max": 1000}, "peak": {"max": 4096}}}]}])
    assert rc == 0, out
    rec, d = record(tmp, "job")
    [step] = rec["steps"]
    assert step["passed"] and step["values"] == {"rc": 0, "ms": 12, "peak": 900}, step
    assert "job: 3 parts" in step["output"] and step["output"].endswith("peak 900 B"), step
    assert asked["job"] == 1, "sent once, not asked again while it waited"
    assert "job done: rc 0" in open(os.path.join(d, "01-run-the-job.txt")).read()


def test_a_limit_the_later_line_breaks_fails_the_step_with_the_reason(bench):
    tmp, sock, asked = bench
    rc, out = run(tmp, sock, [{"name": "job", "steps": [
        {"name": "job", "board": "n1", "cmd": "job start",
         "await": {"pattern": JOB, "within": 10}, "limits": {"peak": {"max": 512}}}]}])
    assert rc != 0 and "failed: job" in out, out
    rec, _ = record(tmp, "job")
    assert rec["steps"][0]["why"] == ["peak 900, over 512"], rec


def test_a_step_of_several_commands_runs_them_in_order_and_stops_at_the_first_failure(bench):
    tmp, sock, asked = bench
    rc, out = run(tmp, sock, [{"name": "send", "steps": [
        {"name": "parts", "board": "n1",
         "cmds": [{"cmd": "part 1", "expect": "^part 1/3"}, "part 2",
                  {"cmd": "part 3", "expect": "^part 3/3"}]}]}])
    assert rc == 0, out
    rec, d = record(tmp, "send")
    [step] = rec["steps"]
    assert step["passed"] and step["commands"] == 3 and asked["parts"] == [1, 2, 3], step
    assert step["output"].splitlines()[:2] == ["> part 1", "part 1/3"], step
    assert sorted(os.listdir(d)) == ["01-parts.txt", "scenario.json"]
    asked["parts"] = []
    rc, out = run(tmp, sock, [{"name": "send", "steps": [
        {"name": "parts", "board": "n1",
         "cmds": [{"cmd": "part 1", "expect": "^part 1/3"}, {"cmd": "part 2", "expect": "^never"},
                  "part 3"]}]}])
    assert rc != 0 and asked["parts"] == [1, 2], (asked, out)

