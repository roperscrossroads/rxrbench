#!/usr/bin/env python3
"""compare.py [--unclean] [--mixed-depth] [--json] <run> [<run>]: what changed between two runs
of a bench's tests, or between the states of a campaign run (`just compare`,
docs/ARCHITECTURE.md#runs-tests-and-campaigns).

A board whose identity one run read from the board and the other took from its last full read
(identity at `spot`, docs/ARCHITECTURE.md#checking-depth) is refused unless --mixed-depth is given.

A run is its directory under runs/ (`<time>`, or a campaign state's `<time>/campaign/<name>/
<nn>-<label>`). With one argument that holds a campaign (campaign.json in it, or in
`<it>/campaign/*/`), each state is compared with the one before it. Reads files, writes none.
"""
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(REPO, "runs")


class CompareError(Exception):
    pass


def _json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_dir(name, runs=None):
    runs = runs or RUNS
    path = name if os.path.isabs(name) else os.path.join(runs, name)
    if not os.path.isdir(path):
        raise CompareError(f"no run {name!r} in {runs}")
    return os.path.normpath(path)


def results(path):
    """{key: result} of one run: ("device test", board, name) -> {passed}; ("scenario", name)
    -> {passed, bindings, steps: [(name, passed)]}. Only this run's own files, not a nested
    campaign's states."""
    out = {}
    for f in sorted(glob.glob(os.path.join(path, "*", "test.json"))):
        rec = _json(f) or {}
        for r in rec.get("results") or []:
            out[("device test", rec.get("device") or os.path.basename(os.path.dirname(f)), r.get("name"))] = \
                {"passed": bool(r.get("passed"))}
    for f in sorted(glob.glob(os.path.join(path, "scenario", "*", "scenario.json"))):
        rec = _json(f) or {}
        out[("scenario", rec.get("scenario") or os.path.basename(os.path.dirname(f)))] = {
            "passed": bool(rec.get("passed")), "bindings": rec.get("bindings"),
            "steps": [(s.get("name"), bool(s.get("passed"))) for s in rec.get("steps") or []]}
    return out


def _first_step_differs(a, b):
    for i in range(max(len(a), len(b))):
        sa = a[i] if i < len(a) else None
        sb = b[i] if i < len(b) else None
        if sa != sb:
            return (sa or sb)[0]
    return None


def compare_results(ra, rb, a_name, b_name):
    rows = []
    for key in sorted(set(ra) | set(rb), key=lambda k: tuple(str(x) for x in k)):
        x, y = ra.get(key), rb.get(key)
        row = {"what": key[0], "name": " ".join(str(k) for k in key[1:]),
               "a": None if x is None else ("passed" if x["passed"] else "failed"),
               "b": None if y is None else ("passed" if y["passed"] else "failed")}
        if x is None or y is None:
            row["verdict"] = f"only in {os.path.basename(a_name if y is None else b_name)}"
        elif key[0] == "scenario" and (x.get("bindings") or None) != (y.get("bindings") or None):
            row["verdict"] = "not comparable"
            row["why"] = f"bound to other boards ({x.get('bindings')} / {y.get('bindings')})"
        elif x["passed"] == y["passed"]:
            row["verdict"] = "same"
            if key[0] == "scenario" and x["steps"] != y["steps"]:
                row["why"] = f"steps differ from {_first_step_differs(x['steps'], y['steps'])}"
        else:
            row["verdict"] = "changed"
            if key[0] == "scenario":
                row["why"] = f"first step that differs: {_first_step_differs(x['steps'], y['steps'])}"
        rows.append(row)
    return rows


def _identity(board):
    rep = board.get("reported")
    rep = {k: v for k, v in rep.items() if v is not None} if isinstance(rep, dict) else rep
    foreign = board.get("foreign")
    return {"declared": (board.get("declared") or {}).get("image"), "reported": rep,
            "foreign": foreign if isinstance(foreign, list) else None}


def state_diff(sa, sb):
    """[{board, a, b}] of the boards whose declared image, reported identity or chip findings
    (what the survey found there that the layout does not own) differ."""
    ba, bb = (sa or {}).get("boards") or {}, (sb or {}).get("boards") or {}
    out = []
    for name in sorted(set(ba) | set(bb)):
        ia = _identity(ba[name]) if name in ba else None
        ib = _identity(bb[name]) if name in bb else None
        if ia != ib:
            out.append({"board": name, "a": ia, "b": ib})
    return out


def identity_depths(st):
    """{board: "full" | "spot"} for each board of a state record whose identity read says its
    depth (a record from before identity_checked existed says nothing: full)."""
    out = {}
    for name, b in ((st or {}).get("boards") or {}).items():
        ic = b.get("identity_checked") if isinstance(b, dict) else None
        out[name] = (ic or {}).get("depth") or "full"
    return out


def compare(a, b, unclean=False, runs=None, mixed_depth=False):
    """The comparison of two runs: {a, b, warnings, state, rows}. Raises CompareError when a run
    is not clean (or has no state record) and `unclean` is not given, or when a board's
    identity was read at another depth in each run and `mixed_depth` is not given."""
    pa, pb = run_dir(a, runs), run_dir(b, runs)
    sa, sb = _json(os.path.join(pa, "state.json")), _json(os.path.join(pb, "state.json"))
    warnings = []
    for name, st in ((a, sa), (b, sb)):
        why = ("has no state record (state.json): it was not started by just run or a campaign"
               if st is None else None if st.get("clean") else
               "was not clean: the bench differed from its declaration")
        if why:
            if not unclean:
                raise CompareError(f"run {name} {why}; --unclean compares it anyway")
            warnings.append(f"run {name} {why}")
    da, db = identity_depths(sa), identity_depths(sb)
    mixed = sorted(n for n in set(da) & set(db) if da[n] != db[n])
    if mixed:
        why = (f"board(s) {', '.join(mixed)}: identity read from the board in one run and "
               "taken from its last full read in the other")
        if not mixed_depth:
            raise CompareError(f"{why}; --mixed-depth compares them anyway")
        warnings.append(why)
    for name, d in ((a, da), (b, db)):
        spot = sorted(n for n, v in d.items() if v == "spot")
        if spot:
            warnings.append(f"run {name}: identity of {', '.join(spot)} from the last full read, "
                            "not asked (the tests ran on the recorded firmware)")
    return {"a": a, "b": b, "warnings": warnings, "state": state_diff(sa, sb),
            "rows": compare_results(results(pa), results(pb), a, b)}


def campaign_pairs(name, runs=None):
    """[(state, next state)] of a campaign run, as run names, in order."""
    path = run_dir(name, runs)
    found = [path] if os.path.exists(os.path.join(path, "campaign.json")) else \
        sorted(os.path.dirname(p) for p in glob.glob(os.path.join(path, "campaign", "*", "campaign.json")))
    if len(found) != 1:
        raise CompareError(f"{name}: {'no campaign' if not found else 'more than one campaign'} in it; "
                           "name the campaign's directory, or two runs")
    rec = _json(os.path.join(found[0], "campaign.json")) or {}
    root = runs or RUNS
    dirs = [os.path.relpath(s["dir"], root) if os.path.isabs(s.get("dir", "")) else s.get("dir")
            for s in rec.get("states") or []]
    if len(dirs) < 2:
        raise CompareError(f"{name}: the campaign ran {len(dirs)} state(s); nothing to compare")
    return list(zip(dirs, dirs[1:]))


def _short(v):
    s = str(v)
    return s[:12] if len(s) == 64 and all(ch in "0123456789abcdef" for ch in s) else s


def board_change(s):
    """One line for a board whose identity differs: the declared image and each reported field
    that changed (a hash cut to 12 characters)."""
    a, b = s["a"], s["b"]
    if a is None or b is None:
        return f"{s['board']}: {'only in the second run' if a is None else 'only in the first run'}"
    parts = []
    if a["declared"] != b["declared"]:
        parts.append(f"image {a['declared']} -> {b['declared']}")
    ra, rb = a["reported"], b["reported"]
    if isinstance(ra, dict) and isinstance(rb, dict):
        for k in sorted(set(ra) | set(rb)):
            if ra.get(k) != rb.get(k):
                parts.append(f"{k} {_short(ra.get(k))} -> {_short(rb.get(k))}")
    elif ra != rb:
        parts.append(f"reported {ra} -> {rb}")
    if a.get("foreign") != b.get("foreign"):
        count = lambda f: "unknown" if f is None else f"{len(f)} region(s)"  # noqa: E731
        parts.append(f"not the layout's on the chip: {count(a.get('foreign'))} -> {count(b.get('foreign'))}")
    return f"{s['board']}: " + "; ".join(parts)


def render(c):
    lines = [f"compare {c['a']} -> {c['b']}"]
    lines += [f"  NOTE: {w}" for w in c["warnings"]]
    if c["state"]:
        lines.append("  the bench:")
        for s in c["state"]:
            lines.append(f"    {board_change(s)}")
    else:
        lines.append("  the bench: the same boards, images and identities")
    width = max([len(f"{r['what']} {r['name']}") for r in c["rows"]] + [10])
    for r in c["rows"]:
        lines.append(f"  {(r['what'] + ' ' + r['name']).ljust(width)}  {str(r['a']):7} -> {str(r['b']):7}  "
                     f"{r['verdict']}" + (f" ({r['why']})" if r.get("why") else ""))
    if not c["rows"]:
        lines.append("  no results in either run")
    return "\n".join(lines)


def main(argv, runs=None, out=sys.stdout):
    unclean = "--unclean" in argv
    mixed_depth = "--mixed-depth" in argv
    as_json = "--json" in argv
    names = [a for a in argv if not a.startswith("--")]
    if len(names) not in (1, 2):
        print(__doc__.splitlines()[0], file=sys.stderr)
        return 2
    try:
        pairs = [tuple(names)] if len(names) == 2 else campaign_pairs(names[0], runs)
        comparisons = [compare(a, b, unclean, runs, mixed_depth) for a, b in pairs]
    except CompareError as e:
        print(f"compare: {e}", file=sys.stderr)
        return 2
    if as_json:
        json.dump(comparisons, out, indent=2)
        out.write("\n")
    else:
        out.write("\n\n".join(render(c) for c in comparisons) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
