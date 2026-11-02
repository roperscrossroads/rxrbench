#!/usr/bin/env python3
"""run.py <layout> [ansible-playbook args ...]: a run of a bench's tests that records its state
(`just run`, docs/ARCHITECTURE.md#runs-tests-and-campaigns).

One run name (`bench_run`, a UTC time) for three plays:
1. the plan of the layout, with the `rxrbench.core.state` callback, which writes
   `runs/<run>/state.json`: what each board was declared and reported, the plan's change count,
   whether it answered, the tool's and the site's commits, and `clean`;
2. the layout's device tests (`bench_tests`), when it declares any;
3. its scenarios (`bench_scenarios`, and the library tests it binds, `bench_test_bindings`),
   when it has any.

The tests run whether the plan was clean or not: a run on a bench that differs from its
declaration is still evidence, and its state.json says it is not clean. The exit status is the
worst of the three plays.
"""
import datetime
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402

VENV = os.path.join(REPO, "venv", "bin")
PLAYBOOKS = os.path.join("collections", "ansible_collections", "rxrbench", "core", "playbooks")


def where(layout):
    return subprocess.run([sys.executable, os.path.join(REPO, "tools", "where.py"), layout],
                          cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()


def declares(inventory_json):
    """(tests, scenarios): does any board of the layout declare device tests, scenarios (inline,
    or library tests it binds)?"""
    hostvars = (inventory_json.get("_meta") or {}).get("hostvars") or {}
    boards = set((inventory_json.get("boards") or {}).get("hosts") or [])
    hv = [v for h, v in hostvars.items() if h in boards]
    return (any(v.get("bench_tests") for v in hv),
            any(v.get("bench_scenarios") or v.get("bench_test_bindings") for v in hv))


def play(layout_dir, playbook, run, args, env=None):
    cmd = [os.path.join(VENV, "ansible-playbook"), "-i", layout_dir,
           os.path.join(PLAYBOOKS, playbook), "-e", json.dumps({"bench_run": run}), *args]
    return subprocess.call(cmd, cwd=REPO, env={**os.environ, **(env or {})})


def main(argv):
    if not argv:
        sys.exit(__doc__.splitlines()[0])
    site.load_env()
    layout_dir, args = where(argv[0]), argv[1:]
    run = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    inv = json.loads(subprocess.run([os.path.join(VENV, "ansible-inventory"), "-i", layout_dir, "--list"],
                                    cwd=REPO, capture_output=True, text=True, check=True).stdout)
    tests, scenarios = declares(inv)
    if not tests and not scenarios:
        sys.exit(f"{argv[0]}: declares no bench_tests, no bench_scenarios and no bench_test_bindings: "
                 "a run would test nothing")
    rcs = {"plan": play(layout_dir, "apply.yml", run, ["--check", "--diff", *args],
                        {"ANSIBLE_CALLBACKS_ENABLED": "rxrbench.core.state,rxrbench.core.timing"})}
    if tests:
        rcs["device tests"] = play(layout_dir, "test.yml", run, args)
    if scenarios:
        rcs["scenarios"] = play(layout_dir, "scenario.yml", run, args)
    print(f"run {run}: " + ", ".join(f"{k} {'ok' if rc == 0 else f'failed ({rc})'}" for k, rc in rcs.items()))
    return max(rcs.values())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
