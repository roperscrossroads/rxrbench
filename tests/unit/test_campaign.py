"""Campaigns (docs/ARCHITECTURE.md#runs-tests-and-campaigns): a state is the site with one layout
changed, read by Ansible as any layout; per state a plan, the apply, a clean plan and the tests;
a failed apply or a bench not clean after it stops the campaign."""
import json
import os
import sys

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "tools"))
import campaign  # noqa: E402  pylint: disable=wrong-import-position

ZSIM = os.path.join(REPO, "site.example", "layouts", "zsim")


def test_a_campaigns_shape_is_checked_before_anything_runs():
    assert campaign.check({}) == ["a campaign is a mapping with `states:`, a list of {state: <layout>, ...}"]
    probs = campaign.check({"states": [
        {"state": "zsim"}, {"state": "zsim"}, {"label": "x"}, {"state": "nope", "label": "n"},
        {"state": "zsim", "label": "g", "vars": {"bench_apply_allowed": True},
         "boards": {"zb1": {"bench_first_flash": True}}, "colour": 1}]})
    assert "state 2: label 'zsim' is another state's (give each a `label`)" in probs
    assert "state 3: names no layout (`state: <layout>`)" in probs
    assert any(p.startswith("state 4: no layout 'nope'") for p in probs)
    assert "state 5: 'colour' is not a state key (state, label, vars, boards, tests, device_tests)" in probs
    assert ("state 5: vars sets bench_apply_allowed: a go-ahead is the layout's own, never a "
            "campaign's (declare it in the layout)") in probs
    assert any(p.startswith("state 5: boards.zb1 sets bench_first_flash") for p in probs)


def test_a_state_is_the_site_with_one_layout_changed(tmp_path):
    st = {"state": "zsim", "boards": {"zb1": {"firmware": {"family": "bare", "image": "zephyr-shell-b"}}},
          "vars": {"bench_tests": []}}
    mine = campaign.make_state(ZSIM, st, str(tmp_path / "s"))
    view = tmp_path / "s" / "site"
    assert os.path.islink(view / "layers") and os.path.islink(view / "tests")
    assert os.path.islink(view / "layouts" / "zsim-vm") and not os.path.islink(mine)
    cfg = yaml.safe_load(open(os.path.join(mine, "devreg.yml")))
    assert cfg["registry"] == os.path.join(REPO, "site.example", "sim", "registry.yaml")
    hv = yaml.safe_load(open(os.path.join(mine, "host_vars", "zb1.yml")))
    assert hv["firmware"]["image"] == "zephyr-shell-b"
    gv = yaml.safe_load(open(os.path.join(mine, "group_vars", "all.yml")))
    assert gv["bench_tests"] == [] and gv["bench_apply_allowed"] is True, "the layout's own go-ahead"
    assert yaml.safe_load(open(os.path.join(ZSIM, "host_vars", "zb1.yml")))["firmware"]["image"] == "zephyr-shell", \
        "the layout on disk is untouched"
    # Ansible reads it as a layout: layers, the library and relative paths all resolve
    inv = campaign.inventory(mine)
    zb1 = inv["_meta"]["hostvars"]["zb1"]
    plain = lambda v: v.get("__ansible_unsafe", v) if isinstance(v, dict) else v  # noqa: E731
    assert plain(zb1["firmware"]["image"]) == "zephyr-shell-b" and plain(zb1["bench_layout"]) == "zsim"
    assert zb1["bench_test_bindings"] == {"zephyr-shells-answer": {"a": "zb1", "b": "zb3"}}


class Plays:
    """A fake of campaign.play: records each call, fails what it is told to, writes state.json."""
    def __init__(self, runs, fail=(), dirty=False):
        self.calls, self.fail, self.dirty, self.runs = [], set(fail), dirty, runs

    def __call__(self, layout_dir, playbook, run, args, env=None, extra=None):
        kind = ("plan after" if "rxrbench.core.state" in (env or {}).get("ANSIBLE_CALLBACKS_ENABLED", "")
                else "plan" if "--check" in args else
                {"apply.yml": "apply", "test.yml": "device tests", "scenario.yml": "scenarios"}[playbook])
        label = run.split("/")[3]
        self.calls.append((label, kind, extra))
        if kind == "plan after":
            os.makedirs(os.path.join(self.runs, run), exist_ok=True)
            with open(os.path.join(self.runs, run, "state.json"), "w") as f:
                json.dump({"clean": not self.dirty}, f)
        return 2 if (label, kind) in self.fail else 0


DOC = {"states": [{"state": "zsim", "label": "a"},
                  {"state": "zsim", "label": "b", "tests": ["zephyr-shells-answer"], "device_tests": False},
                  {"state": "zsim", "label": "c"}]}


@pytest.fixture
def camp(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(campaign, "campaign_path", lambda name: "/x/" + name + ".yml")
    monkeypatch.setattr(campaign, "_read", lambda p, real=campaign._read: DOC if p.startswith("/x/") else real(p))
    return tmp_path


def record(tmp_path):
    [p] = [os.path.join(d, "campaign.json") for d, _s, f in os.walk(tmp_path / "runs") if "campaign.json" in f]
    return json.load(open(p))


def test_each_state_is_planned_applied_checked_clean_and_tested(camp):
    plays = Plays(str(camp / "runs"))
    assert campaign.main(["c"], plays=plays, now="T1") == 0
    assert [(lbl, k) for lbl, k, _e in plays.calls] == [
        (f"{n}-{s}", k) for n, s in (("01", "a"),) for k in ("plan", "apply", "plan after", "device tests", "scenarios")
    ] + [("02-b", k) for k in ("plan", "apply", "plan after", "scenarios")] + [
        ("03-c", k) for k in ("plan", "apply", "plan after", "device tests", "scenarios")]
    assert ("02-b", "scenarios", {"bench_scenario_only": ["zephyr-shells-answer"]}) in plays.calls
    rec = record(camp)
    assert rec["passed"] and [s["label"] for s in rec["states"]] == ["a", "b", "c"]
    assert all(s["clean"] for s in rec["states"])


def test_a_failed_apply_stops_the_campaign(camp):
    plays = Plays(str(camp / "runs"), fail={("02-b", "apply")})
    assert campaign.main(["c"], plays=plays, now="T2") == 1
    assert not any(lbl == "03-c" for lbl, _k, _e in plays.calls), "no state after the failed one"
    assert not any(k == "plan after" and lbl == "02-b" for lbl, k, _e in plays.calls)
    rec = record(camp)
    assert not rec["passed"] and rec["states"][-1]["stopped"] == "the apply failed"


def test_a_bench_not_clean_after_its_apply_stops_before_its_tests(camp):
    plays = Plays(str(camp / "runs"), dirty=True)
    assert campaign.main(["c"], plays=plays, now="T3") == 1
    assert [k for _l, k, _e in plays.calls] == ["plan", "apply", "plan after"]
    assert record(camp)["states"][0]["stopped"].startswith("the bench is not clean")


def test_a_failed_test_is_recorded_and_the_campaign_goes_on(camp):
    plays = Plays(str(camp / "runs"), fail={("01-a", "scenarios")})
    assert campaign.main(["c"], plays=plays, now="T4") == 1
    rec = record(camp)
    assert len(rec["states"]) == 3 and rec["states"][0]["steps"]["scenarios"] == 2 and not rec["passed"]


def test_the_example_campaign_is_well_formed():
    import yaml as y
    doc = y.safe_load(open(os.path.join(REPO, "site.example", "campaigns", "zsim-images.yml")))
    assert campaign.check(doc) == []
