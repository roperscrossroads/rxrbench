"""Core alone on a simulated bench (docs/ARCHITECTURE.md#core-portability): the plays run from a
collections tree that holds `rxrbench.core` and the fixture plugin only (tools/core_only.py),
against the simulated Zephyr bench (`site.example/layouts/zsim`). Core's own families (`bare`,
`zephyr_mcumgr`) plan, apply and plan 0; the boards whose family a bundled plugin brings are
compared by their image only, and the plan says so."""
import json
import os
import subprocess
import sys

from tests.sim.test_zephyr_sim import AS_B, LAYOUT, REPO, VENV, bench, pytestmark, recap  # noqa: F401  pylint: disable=unused-import

sys.path.insert(0, os.path.join(REPO, "tools"))
import core_only  # noqa: E402  pylint: disable=wrong-import-position

PLUGIN_BOARDS = ("zb3", "zb4", "nb1")   # the zephyr and nuttx plugins' families


def play(run, playbook, *args):
    tree = core_only.build()
    env = {**core_only.env(tree), "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0",
           "RXRBENCH_SITE": "site.example", "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", LAYOUT,
                        os.path.join("collections/ansible_collections/rxrbench/core/playbooks", playbook), *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=300, check=False)
    return r.returncode, r.stdout + r.stderr


def test_core_alone_plans_the_bench_and_says_what_it_did_not_compare(bench):
    rc, out = play(bench, "apply.yml", "--check", "--diff")
    assert rc == 0, out
    for name, counts in recap(out).items():
        assert counts["changed"] == 0 and counts["failed"] == 0, (name, out)
    flat = " ".join(out.split())
    for name in PLUGIN_BOARDS:
        assert f"{name}: no loaded plugin brings its family" in flat, out
    assert "zb1: no loaded plugin" not in flat and "zb5: no loaded plugin" not in flat, out


def test_core_alone_writes_its_own_family_and_plans_0_after(bench):
    rc, out = play(bench, "apply.yml", "--check", "--diff", *AS_B)
    assert rc == 0, out
    assert recap(out)["zb1"]["changed"] == 1, out
    rc, out = play(bench, "apply.yml", *AS_B)
    assert rc == 0, out
    assert recap(out)["zb1"]["changed"] == 1 and recap(out)["zb1"]["failed"] == 0, out
    rc, out = play(bench, "apply.yml", "--check", "--diff", *AS_B)
    assert rc == 0, out
    assert recap(out)["zb1"]["changed"] == 0, out


def test_core_alone_runs_scenario_steps_on_its_own_boards(bench):
    scenarios = [{"name": "core-alone", "steps": [
        {"board": "zb1", "cmd": "kernel version", "expect": "^Zephyr version"},
        {"board": "zb2", "cmd": "kernel uptime", "expect": "^Uptime: [0-9]+ ms", "within": 5}]}]
    rc, out = play(bench, "scenario.yml", "-e", json.dumps({"bench_scenarios": scenarios}))
    assert rc == 0, out
    assert "failed: core-alone" not in out, out
