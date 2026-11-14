#!/usr/bin/env python3
"""matrix.py [--records DIR] [--out FILE] [--check] [--stdout]: the capability matrix's table from
the board exercise's records.

Reads every record under `<site>/exercise/` (`tools/exercise.py`'s `runs/<time>/exercise/
<board>.json`, kept as the site's latest per board by `--record` or `RXRBENCH_HW_RECORD=1`),
takes the newest per board, and writes one table, verbs as rows and boards as columns, between
the markers `<!-- exercise-matrix:begin -->` and `<!-- exercise-matrix:end -->` of
`<site>/docs/CAPABILITY-MATRIX.md`; everything outside the markers (the hand-written notes) is
left as it is, and a file without the markers gets them before its first `## ` heading. A cell
says what the record says and no more: `Yes` with the seconds the verb took when it passed,
`**No**` when it failed, `not run` when it applies but was not run (the destructive verbs without
the flag, a verb skipped on request, a write after a failed one), `—` when it does not apply to
the board. Below the table, one line per board: its layout, family, flasher, routes, the run, the
tool and site commits, whether it came back, and the reason behind every `—`, `not run` and `No`.
`--check` exits 1 when the file is not what the records say (for a gate); `--stdout` prints the
table instead of writing. No board or family is named here: the names come from the records.
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  pylint: disable=unused-import,wrong-import-position  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402  pylint: disable=wrong-import-position

BEGIN, END = "<!-- exercise-matrix:begin -->", "<!-- exercise-matrix:end -->"
PASS, FAIL, SKIP = "pass", "fail", "skip"
YES, NO, NOT_RUN, NA = "Yes", "**No**", "not run", "—"


def load(records_dir):
    """The newest record per board, in the order of their first run."""
    latest = {}
    for path in sorted(glob.glob(os.path.join(records_dir, "*.json"))):
        with open(path) as fh:
            rec = json.load(fh)
        if rec.get("v") != 1 or "board" not in rec or "steps" not in rec:
            continue
        have = latest.get(rec["board"])
        if have is None or rec.get("run", "") > have.get("run", ""):
            latest[rec["board"]] = rec
    return sorted(latest.values(), key=lambda r: (r.get("run", ""), r["board"]))


def verbs_of(records):
    """Every verb any record has, in the records' own order (the exercise's)."""
    seen = []
    for rec in records:
        for s in rec["steps"]:
            if s["verb"] not in seen:
                seen.append(s["verb"])
    return seen


def cell(step):
    if step is None:
        return ""
    if step["result"] == PASS:
        return f"{YES} ({_secs(step['seconds'])})" if step.get("seconds") else YES
    if step["result"] == FAIL:
        return NO
    return NOT_RUN if step.get("applies") else NA


def _secs(s):
    return f"{s:.0f} s" if s >= 10 else f"{s:.1f} s"


def _when(run):
    try:
        return datetime.datetime.strptime(run, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d %H:%MZ")
    except (TypeError, ValueError):
        return run or "?"


def board_line(rec):
    steps = rec["steps"]
    head = (f"- **{rec['board']}** — layout `{rec['layout']}`, {rec.get('family') or '?'} over "
            f"{rec.get('flasher') or '?'} ({', '.join(rec.get('routes') or []) or 'no route'}); "
            f"{_when(rec.get('run'))}, {rec.get('seconds', 0):.0f} s, tool `{rec.get('tool_commit') or '?'}`, "
            f"site `{rec.get('site_commit') or '?'}`; "
            f"{'with' if rec.get('destructive') else 'without'} `--destructive`; "
            f"{'came back' if rec.get('came_back') else 'came back: not tested (unclean start)' if rec.get('came_back') is None and 'came_back' in rec else 'DID NOT COME BACK'}.")
    parts = []
    failed = [s for s in steps if s["result"] == FAIL]
    if failed:
        parts.append("Failed: " + "; ".join(f"{s['verb']} ({s.get('msg') or 'failed'})" for s in failed))
    not_run = [s for s in steps if s["result"] == SKIP and s.get("applies")]
    if not_run:
        parts.append("Not run: " + "; ".join(_grouped(not_run)))
    na = [s for s in steps if s["result"] == SKIP and not s.get("applies")]
    if na:
        parts.append("Does not apply: " + "; ".join(_grouped(na)))
    return head + (" " + ". ".join(parts) + "." if parts else "")


def _grouped(steps):
    """Verbs with the same reason named together: `a, b (reason)`."""
    by_reason = {}
    for s in steps:
        by_reason.setdefault(s.get("msg") or s.get("reason") or "no reason recorded", []).append(s["verb"])
    return [f"{', '.join(vs)} ({why})" for why, vs in by_reason.items()]


def render(records):
    if not records:
        return "No exercise record yet: `just exercise <layout> <board> --record` writes the first.\n"
    verbs = verbs_of(records)
    kinds = {}
    for rec in records:
        for s in rec["steps"]:
            kinds.setdefault(s["verb"], s.get("kind", ""))
    lines = ["| Verb | Kind | " + " | ".join(r["board"] for r in records) + " |",
             "|---|---|" + "---|" * len(records)]
    for v in verbs:
        cells = [cell(next((s for s in rec["steps"] if s["verb"] == v), None)) for rec in records]
        lines.append(f"| `{v}` | {kinds[v]} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.extend(board_line(rec) for rec in records)
    return "\n".join(lines) + "\n"


def splice(text, body):
    """`text` with the table between the markers replaced by `body`; markers added before the
    first `## ` heading (or at the end) when the file has none."""
    block = f"{BEGIN}\n{body}{END}"
    if BEGIN in text and END in text:
        return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda _m: block, text, count=1, flags=re.S)
    m = re.search(r"^## ", text, re.M)
    at = m.start() if m else len(text)
    return text[:at] + block + "\n\n" + text[at:]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--records", help="the records directory (default <site>/exercise)")
    ap.add_argument("--out", help="the matrix file (default <site>/docs/CAPABILITY-MATRIX.md)")
    ap.add_argument("--check", action="store_true", help="exit 1 when the file is not what the records say")
    ap.add_argument("--stdout", action="store_true", help="print the table, write nothing")
    a = ap.parse_args(argv)
    site.load_env()
    records_dir = a.records or os.path.join(site.site_dir(), "exercise")
    out = a.out or os.path.join(site.site_dir(), "docs", "CAPABILITY-MATRIX.md")
    body = render(load(records_dir))
    if a.stdout:
        print(body, end="")
        return 0
    text = ""
    if os.path.isfile(out):
        with open(out) as fh:
            text = fh.read()
    new = splice(text, body)
    if a.check:
        if new != text:
            print(f"{out}: not what {records_dir} says; run tools/matrix.py", file=sys.stderr)
            return 1
        print(f"{out}: up to date")
        return 0
    if new == text:
        print(f"{out}: unchanged")
        return 0
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as fh:
        fh.write(new)
    print(f"{out}: written from {records_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
