"""The board exercise on the simulated Zephyr bench: zb3, the settings board (config through its
shell, a key it made, two more images staged), goes through every verb that applies to it (facts,
plan, secrets, a chip backup, the key escrowed, a no-op apply, a snapshot and the switch back, an
update to its second image and back, the key restore planned, and with --destructive the chip
erased and restored), the verbs that do not apply are recorded with their reasons, and the board
comes back as it was. Left out on the simulated bench: the survey (the simulated host has no USB
devices to read) and the reboot (a native_sim process is not seen to reboot by its broker)."""
import json
import os
import shutil
import subprocess

import pytest

from tests.sim.test_zephyr_app_sim import site  # noqa: F401  pylint: disable=unused-import  (a site with an encrypted store)
from tests.sim.test_zephyr_sim import REPO, VENV, bench  # noqa: F401  pylint: disable=unused-import

pytestmark = pytest.mark.skipif(not shutil.which("sops") or not shutil.which("age-keygen"),
                                reason="needs sops and age for the site's encrypted store")
SKIPPED_ON_SIM = "survey,survey-deep,reboot"


def _env(run, env):
    return {**os.environ, **env, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0",
            "PATH": VENV + os.pathsep + os.environ["PATH"]}


def exercise(run, env, board, *args):
    r = subprocess.run([os.path.join(VENV, "python"), "tools/exercise.py", "zsim", board, "confirm=apply", "--json", *args],
                       cwd=REPO, env=_env(run, env), capture_output=True, text=True, timeout=1500, check=False)
    try:
        rec = json.loads(r.stdout)
    except ValueError:
        raise AssertionError(r.stdout + r.stderr)  # pylint: disable=raise-missing-from
    return r.returncode, r.stdout + r.stderr, rec


def applied(run, env, board):
    """A freshly started simulated board holds nothing yet: the exercise starts from a board that
    matches its layout, as a bench does."""
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", "site.example/layouts/zsim",
                        "collections/ansible_collections/rxrbench/core/playbooks/apply.yml", "--limit", board],
                       cwd=REPO, env=_env(run, env), capture_output=True, text=True, timeout=600, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def failures(rec):
    return json.dumps([s for s in rec["steps"] if s["result"] == "fail"], indent=1)


def test_zb3_goes_through_every_verb_that_applies_and_comes_back(bench, site):  # noqa: F811  pylint: disable=redefined-outer-name
    applied(bench, site, "zb3")
    rc, out, rec = exercise(bench, site, "zb3", "--destructive", "--skip", SKIPPED_ON_SIM)
    steps = {s["verb"]: s for s in rec["steps"]}
    assert rc == 0 and rec["passed"] and rec["came_back"], out + "\n" + failures(rec)
    ran = {v for v, s in steps.items() if s["result"] == "pass"}
    assert ran >= {"facts", "plan", "secrets", "diag", "backup-chip", "keys-export", "apply", "snapshot-switch",
                   "update-there-and-back", "keys-restore", "erase-chip", "came-back"}, sorted(ran)
    assert steps["survey"]["msg"] == "skipped on request" and steps["reboot"]["msg"] == "skipped on request"
    assert steps["backup-regions"]["result"] == "skip" and steps["backup-regions"]["msg"] == "the flasher cannot read regions"
    assert steps["locate"]["result"] == "skip" and steps["erase-region"]["result"] == "skip"
    there = steps["update-there-and-back"]["evidence"]
    assert there["there"]["image"] != "zapp-1.0" and there["plan_back"] == 0, there
    with open(rec["path"]) as f:
        assert json.load(f)["board"] == "zb3"
    d = os.path.join(REPO, "runs", rec["run"], "exercise", "zb3")
    assert any(n.endswith("-facts") for n in os.listdir(d)) and any(n.endswith("-plan-end") for n in os.listdir(d))


def own_site(site, apply_allowed=True):  # noqa: F811  pylint: disable=redefined-outer-name
    """The site's own copy of the example layouts (zsim copied, so it can be edited), the rest linked."""
    root = site["RXRBENCH_SITE"]
    for entry in os.listdir(os.path.join(REPO, "site.example")):   # the layout's relative paths (../../sim) resolve
        if entry not in ("layouts", "plugins.yml", "identity") and not os.path.exists(os.path.join(root, entry)):
            os.symlink(os.path.join(REPO, "site.example", entry), os.path.join(root, entry))
    os.makedirs(os.path.join(root, "identity"), exist_ok=True)   # this site's own escrow, under its own key
    layouts = os.path.join(root, "layouts")
    os.makedirs(layouts)
    for entry in os.listdir(os.path.join(REPO, "site.example", "layouts")):
        if entry != "zsim":
            os.symlink(os.path.join(REPO, "site.example", "layouts", entry), os.path.join(layouts, entry))
    shutil.copytree(os.path.join(REPO, "site.example", "layouts", "zsim"), os.path.join(layouts, "zsim"))
    gv = os.path.join(layouts, "zsim", "group_vars", "all.yml")
    with open(gv) as f:
        text = f.read()
    assert "bench_apply_allowed: true" in text
    if not apply_allowed:
        with open(gv, "w") as f:
            f.write(text.replace("bench_apply_allowed: true", "bench_apply_allowed: false"))
    return root


def test_a_board_without_the_go_ahead_gets_the_reads_alone(bench, site):  # noqa: F811  pylint: disable=redefined-outer-name
    """The site's own copy of the layout, with the go-ahead off: the reads and the backups run,
    nothing that writes the board does, and the record says why for each."""
    own_site(site, apply_allowed=False)
    applied(bench, site, "zb3")
    rc, out, rec = exercise(bench, site, "zb3", "--destructive", "--skip", "survey")
    steps = {s["verb"]: s for s in rec["steps"]}
    assert rc == 0 and rec["passed"], out + "\n" + failures(rec)
    assert steps["backup-chip"]["result"] == "pass" and steps["plan"]["result"] == "pass" and steps["keys-export"]["result"] == "pass"
    for v in ("apply", "snapshot-switch", "update-there-and-back", "erase-chip", "reboot"):
        assert steps[v]["result"] == "skip" and steps[v]["msg"] == "the layout gives this board no bench_apply_allowed", v


def test_a_second_run_with_stale_only_leaves_out_what_passed_and_the_ledger_keeps_both(bench, site):  # noqa: F811  pylint: disable=redefined-outer-name
    """--record keeps each step in the site's ledger; with nothing changed, --stale-only runs the start
    plan and came-back and leaves every verb that passed out as fresh."""
    root = own_site(site)
    applied(bench, site, "zb3")
    rc, out, first = exercise(bench, site, "zb3", "--skip", SKIPPED_ON_SIM, "--record")
    assert rc == 0 and first["passed"] and first["v"] == 2, out + "\n" + failures(first)
    assert first["board_tag"] and first["firmware"]["id"] != "unknown"
    ledger_path = os.path.join(root, "exercise", "ledger.jsonl")
    with open(ledger_path) as f:
        n_first = len(f.read().splitlines())
    passed = {s["verb"] for s in first["steps"] if s["result"] == "pass"}
    rc, out, second = exercise(bench, site, "zb3", "--skip", SKIPPED_ON_SIM, "--stale-only", "--record")
    assert rc == 0 and second["passed"], out + "\n" + failures(second)
    steps = {s["verb"]: s for s in second["steps"]}
    assert steps["plan"]["result"] == "pass" and steps["came-back"]["result"] == "pass"
    left_out = {v for v, s in steps.items() if s["result"] == "skip" and (s.get("msg") or "").startswith("fresh:")}
    assert left_out == passed - {"plan", "came-back"}, (left_out, passed)
    assert "apply" in left_out and "snapshot-switch" in left_out
    with open(ledger_path) as f:
        assert len(f.read().splitlines()) == n_first + 2, "the second run adds its plan and came-back"
