"""Compare (docs/ARCHITECTURE.md#runs-tests-and-campaigns): per device test and scenario same,
changed, only in one run or not comparable, beside what changed on the bench; a run that was
not clean is refused unless asked."""
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "tools"))
import compare  # noqa: E402  pylint: disable=wrong-import-position


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def a_run(runs, name, image="img-a", clean=True, uptime=True, steps=(True, True), bindings=None,
          tests=True):
    d = os.path.join(runs, name)
    write(os.path.join(d, "state.json"), {"clean": clean, "boards": {
        "b1": {"declared": {"image": image}, "reported": {"family": "bare", "hash": ("a" if image == "img-a" else "b") * 64}},
        "b2": {"declared": {"image": "img-x"}, "reported": {"family": "bare", "hash": "c" * 64}}}})
    if tests:
        write(os.path.join(d, "b1", "test.json"), {"device": "b1", "results": [
            {"name": "version", "passed": True}, {"name": "uptime", "passed": uptime}]})
    write(os.path.join(d, "scenario", "talk", "scenario.json"), {
        "scenario": "talk", "passed": all(steps), "bindings": bindings,
        "steps": [{"name": f"0{i + 1}-s", "passed": p} for i, p in enumerate(steps)]})
    return name


def rows(c):
    return {(r["what"], r["name"]): r for r in c["rows"]}


def test_same_changed_and_what_changed_on_the_bench(tmp_path):
    runs = str(tmp_path)
    a = a_run(runs, "A")
    b = a_run(runs, "B", image="img-b", uptime=False, steps=(True, False))
    c = compare.compare(a, b, runs=runs)
    r = rows(c)
    assert r[("device test", "b1 version")]["verdict"] == "same"
    assert r[("device test", "b1 uptime")] == {"what": "device test", "name": "b1 uptime", "a": "passed",
                                               "b": "failed", "verdict": "changed"}
    assert r[("scenario", "talk")]["verdict"] == "changed"
    assert r[("scenario", "talk")]["why"] == "first step that differs: 02-s"
    assert [s["board"] for s in c["state"]] == ["b1"], "b2 is the same on both"
    assert compare.board_change(c["state"][0]) == "b1: image img-a -> img-b; hash aaaaaaaaaaaa -> bbbbbbbbbbbb"


def test_a_result_in_one_run_only_or_on_other_boards(tmp_path):
    runs = str(tmp_path)
    a = a_run(runs, "A", bindings={"x": "b1"})
    b = a_run(runs, "B", bindings={"x": "b2"}, tests=False)
    r = rows(compare.compare(a, b, runs=runs))
    assert r[("device test", "b1 uptime")]["verdict"] == "only in A"
    assert r[("scenario", "talk")]["verdict"] == "not comparable"
    assert "bound to other boards" in r[("scenario", "talk")]["why"]


def test_a_run_that_was_not_clean_is_compared_only_when_asked(tmp_path, capsys):
    runs = str(tmp_path)
    a = a_run(runs, "A")
    b = a_run(runs, "B", clean=False)
    os.makedirs(os.path.join(runs, "C"))
    assert compare.main([a, b], runs=runs) == 2
    assert "run B was not clean" in capsys.readouterr().err
    assert compare.main([a, "C"], runs=runs) == 2
    assert "run C has no state record" in capsys.readouterr().err
    out = io.StringIO()
    assert compare.main(["--unclean", a, b], runs=runs, out=out) == 0
    assert out.getvalue().splitlines()[1] == "  NOTE: run B was not clean: the bench differed from its declaration"


def test_a_campaign_run_compares_each_state_with_the_one_before(tmp_path):
    runs = str(tmp_path)
    base = "T/campaign/c"
    names = [a_run(runs, f"{base}/0{i}-{lbl}", image=img) for i, (lbl, img) in
             enumerate((("a", "img-a"), ("b", "img-b"), ("back", "img-a")), 1)]
    write(os.path.join(runs, base, "campaign.json"),
          {"states": [{"dir": os.path.join(runs, n)} for n in names]})
    assert compare.campaign_pairs("T", runs) == list(zip(names, names[1:]))
    out = io.StringIO()
    assert compare.main(["--json", "T"], runs=runs, out=out) == 0
    got = json.loads(out.getvalue())
    assert [(c["a"], c["b"]) for c in got] == list(zip(names, names[1:]))
    assert all(r["verdict"] == "same" for c in got for r in c["rows"])
    assert [s["board"] for c in got for s in c["state"]] == ["b1", "b1"]


def test_a_name_that_is_no_run_is_said(tmp_path, capsys):
    assert compare.main(["nope", "nada"], runs=str(tmp_path)) == 2
    assert "no run 'nope'" in capsys.readouterr().err


def test_chip_findings_that_differ_are_said_beside_the_results(tmp_path):
    s = {"board": "c1", "a": {"declared": "l", "reported": {"v": "1"}, "foreign": []},
         "b": {"declared": "l", "reported": {"v": "1"}, "foreign": ["wifite"]}}
    assert compare.board_change(s) == "c1: not the layout's on the chip: 0 region(s) -> 1 region(s)"


def _identity_depth(runs, name, depth):
    p = os.path.join(runs, name, "state.json")
    with open(p) as f:
        st = json.load(f)
    st["boards"]["b1"]["identity_checked"] = {"depth": depth, "reason": "asked"}
    write(p, st)


def test_a_board_read_at_another_identity_depth_in_each_run_is_refused_unless_asked(tmp_path):
    runs = str(tmp_path)
    a, b = a_run(runs, "A"), a_run(runs, "B")
    _identity_depth(runs, b, "spot")
    try:
        compare.compare(a, b, runs=runs)
        raise AssertionError("mixed identity depths compared")
    except compare.CompareError as e:
        assert "b1" in str(e) and "--mixed-depth" in str(e)
    c = compare.compare(a, b, runs=runs, mixed_depth=True)
    assert any("last full read" in w and "b1" in w for w in c["warnings"])
    out = io.StringIO()
    assert compare.main([a, b], runs=runs, out=out) == 2
    assert compare.main([a, b, "--mixed-depth"], runs=runs, out=out) == 0


def test_both_runs_at_spot_compare_and_say_so(tmp_path):
    runs = str(tmp_path)
    a, b = a_run(runs, "A"), a_run(runs, "B")
    _identity_depth(runs, a, "spot")
    _identity_depth(runs, b, "spot")
    c = compare.compare(a, b, runs=runs)
    assert [w for w in c["warnings"] if "not asked" in w] == [
        "run A: identity of b1 from the last full read, not asked (the tests ran on the recorded firmware)",
        "run B: identity of b1 from the last full read, not asked (the tests ran on the recorded firmware)"]
