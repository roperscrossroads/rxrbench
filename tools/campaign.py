#!/usr/bin/env python3
"""campaign.py <name> [ansible-playbook args ...]: a declared sequence of bench states, each
applied and tested in turn (`just campaign`, docs/ARCHITECTURE.md#runs-tests-and-campaigns).

The campaign is `<site>/campaigns/<name>.yml` (the site's, then site.example's). Each state is a
layout, optionally with overrides, made as the site with that one layout changed: a copy of
the layout's files under the state's run directory, each override replacing the top-level key
of the file it names, beside links to the site's layers, tests and other layouts. Ansible
reads it as any layout, so it is validated, planned and gated exactly as one on disk.

Per state: a plan; the apply; a plan that records the state (state.json) and must be clean;
then the device tests and the scenarios. A failed apply, or a bench not clean after its apply,
stops the campaign. Results: runs/<time>/campaign/<name>/<nn>-<label>/ and campaign.json.
"""
import datetime
import json
import os
import subprocess
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402

VENV = os.path.join(REPO, "venv", "bin")
#: Where runs are kept (each play's bench_runs_dir default is the same).
RUNS = os.path.join(REPO, "runs")
PLAYBOOKS = os.path.join("collections", "ansible_collections", "rxrbench", "core", "playbooks")
#: The go-aheads: each is one layout's own word, so a campaign may not set one (nor may a layer).
GO_AHEADS = ("bench_apply_allowed", "bench_first_flash")
STATE_KEYS = ("state", "label", "vars", "boards", "tests", "device_tests")


class CampaignError(Exception):
    pass


def campaign_path(name):
    for root in (site.site_dir(), site.EXAMPLE):
        for ext in (".yml", ".yaml"):
            p = os.path.join(root, "campaigns", name + ext)
            if os.path.exists(p):
                return p
    raise CampaignError(f"no campaign {name!r} in " + " or ".join(
        os.path.join(r, "campaigns") for r in (site.site_dir(), site.EXAMPLE)))


def check(doc, layout_of=site.layout_path):
    """Problems with a campaign's shape, before anything runs."""
    out = []
    states = (doc or {}).get("states") if isinstance(doc, dict) else None
    if not isinstance(states, list) or not states:
        return ["a campaign is a mapping with `states:`, a list of {state: <layout>, ...}"]
    labels = set()
    for i, st in enumerate(states, 1):
        if not isinstance(st, dict) or not st.get("state"):
            out.append(f"state {i}: names no layout (`state: <layout>`)")
            continue
        for k in st:
            if k not in STATE_KEYS:
                out.append(f"state {i}: {k!r} is not a state key ({', '.join(STATE_KEYS)})")
        try:
            layout_of(st["state"])
        except FileNotFoundError as e:
            out.append(f"state {i}: {e}")
        label = st.get("label") or st["state"]
        if label in labels:
            out.append(f"state {i}: label {label!r} is another state's (give each a `label`)")
        labels.add(label)
        for where, block in (("vars", st.get("vars") or {}),
                             *((f"boards.{b}", v or {}) for b, v in (st.get("boards") or {}).items())):
            if not isinstance(block, dict):
                out.append(f"state {i}: {where} is a mapping of variables")
                continue
            for k in block:
                if k in GO_AHEADS:
                    out.append(f"state {i}: {where} sets {k}: a go-ahead is the layout's own, "
                               "never a campaign's (declare it in the layout)")
        if st.get("tests") is not None and not isinstance(st["tests"], list):
            out.append(f"state {i}: tests is a list of scenario names")
    return out


def _read(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("---\n# a campaign state: the layout's file with the campaign's overrides\n")
        yaml.safe_dump(data, f, sort_keys=False)


def make_state(layout_dir, st, where):
    """The state's layout: the site with this layout copied (site.state_view), each override
    replacing the top-level key of the file it names; returns the copy's directory."""
    mine = site.state_view(layout_dir, where)
    if st.get("vars"):
        p = os.path.join(mine, "group_vars", "all.yml")
        _write(p, {**_read(p), **st["vars"]})
    for board, over in (st.get("boards") or {}).items():
        d = os.path.join(mine, "host_vars", board)
        if os.path.isdir(d):    # a directory: a last file, read last, replaces the keys
            _write(os.path.join(d, "zz-campaign.yml"), over)
        else:
            p = next((os.path.join(mine, "host_vars", board + e) for e in (".yml", ".yaml")
                      if os.path.exists(os.path.join(mine, "host_vars", board + e))),
                     os.path.join(mine, "host_vars", board + ".yml"))
            _write(p, {**_read(p), **over})
    return mine


def play(layout_dir, playbook, run, args, env=None, extra=None):
    cmd = [os.path.join(VENV, "ansible-playbook"), "-i", layout_dir,
           os.path.join(PLAYBOOKS, playbook), "-e", json.dumps({"bench_run": run, **(extra or {})}), *args]
    print("campaign: " + " ".join(cmd[1:]), flush=True)
    return subprocess.call(cmd, cwd=REPO, env={**os.environ, **(env or {})})


def inventory(layout_dir):
    r = subprocess.run([os.path.join(VENV, "ansible-inventory"), "-i", layout_dir, "--list"],
                       cwd=REPO, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise CampaignError(f"the state does not resolve as a layout:\n{r.stderr.strip()[-2000:]}")
    return json.loads(r.stdout)


def declares(inv):
    """(device tests, scenarios) the state has, as tools/run.py counts them."""
    hostvars = (inv.get("_meta") or {}).get("hostvars") or {}
    boards = set((inv.get("boards") or {}).get("hosts") or [])
    hv = [v for h, v in hostvars.items() if h in boards]
    return (any(v.get("bench_tests") for v in hv),
            any(v.get("bench_scenarios") or v.get("bench_test_bindings") for v in hv))


def run_state(n, st, base, run, args, plays=play):
    """One state: its record ({label, layout, overrides, steps, clean, stopped})."""
    label = st.get("label") or st["state"]
    sub = f"{run}/campaign/{base['name']}/{n:02d}-{label}"
    where = os.path.join(base["runs"], sub)
    rec = {"n": n, "label": label, "layout": st["state"], "dir": where,
           "overrides": {k: st[k] for k in ("vars", "boards") if st.get(k)},
           "steps": {}, "clean": None, "stopped": None}
    try:
        layout_dir = make_state(site.layout_path(st["state"]), st, where)
        tests, scenarios = declares(inventory(layout_dir))
    except (CampaignError, OSError, yaml.YAMLError) as e:
        rec["stopped"] = f"the state could not be made: {e}"
        return rec
    timing = {"ANSIBLE_CALLBACKS_ENABLED": "rxrbench.core.timing"}
    rec["steps"]["plan"] = plays(layout_dir, "apply.yml", sub + "/plan-before", ["--check", "--diff", *args], timing)
    rec["steps"]["apply"] = plays(layout_dir, "apply.yml", sub, args, timing)
    if rec["steps"]["apply"] != 0:
        rec["stopped"] = "the apply failed"
        return rec
    rec["steps"]["plan after"] = plays(layout_dir, "apply.yml", sub, ["--check", "--diff", *args],
                                       {"ANSIBLE_CALLBACKS_ENABLED": "rxrbench.core.state,rxrbench.core.timing"})
    state = _read_json(os.path.join(where, "state.json"))
    rec["clean"] = bool(state.get("clean")) and rec["steps"]["plan after"] == 0
    if not rec["clean"]:
        rec["stopped"] = "the bench is not clean after its apply (state.json)"
        return rec
    if tests and st.get("device_tests", True):
        rec["steps"]["device tests"] = plays(layout_dir, "test.yml", sub, args)
    if scenarios and st.get("tests") != []:
        extra = {"bench_scenario_only": st["tests"]} if st.get("tests") else None
        rec["steps"]["scenarios"] = plays(layout_dir, "scenario.yml", sub, args, extra=extra)
    return rec


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main(argv, plays=play, now=None):
    if not argv:
        sys.exit(__doc__.splitlines()[0])
    site.load_env()
    name, args = argv[0], argv[1:]
    try:
        path = campaign_path(name)
        doc = _read(path)
    except (CampaignError, yaml.YAMLError) as e:
        sys.exit(f"campaign {name}: {e}")
    problems = check(doc)
    if problems:
        sys.exit(f"campaign {name} refused, nothing ran:\n  " + "\n  ".join(problems))
    run = now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = {"name": name, "runs": RUNS}
    states = []
    for n, st in enumerate(doc["states"], 1):
        rec = run_state(n, st, base, run, args, plays)
        states.append(rec)
        print(f"campaign {name}: state {n} {rec['label']}: "
              + ", ".join(f"{k} {'ok' if rc == 0 else f'failed ({rc})'}" for k, rc in rec["steps"].items())
              + (f"; stopped: {rec['stopped']}" if rec["stopped"] else ""), flush=True)
        if rec["stopped"]:
            break
    passed = (len(states) == len(doc["states"]) and not any(s["stopped"] for s in states)
              and all(rc == 0 for s in states for rc in s["steps"].values()))
    out = os.path.join(base["runs"], run, "campaign", name)
    os.makedirs(out, exist_ok=True)
    record = {"v": 1, "campaign": name, "file": path, "run": run, "summary": doc.get("summary"),
              "states": states, "declared": len(doc["states"]), "passed": passed}
    with open(os.path.join(out, "campaign.json"), "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    print(f"campaign {name}: {'passed' if passed else 'FAILED'} ({len(states)} of "
          f"{len(doc['states'])} states run): {os.path.join(out, 'campaign.json')}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
