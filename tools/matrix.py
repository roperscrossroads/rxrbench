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

`--ledger` adds a second table from the site's ledger (tools/ledger.py, every step of every kept
run): verbs as rows, (board type, firmware) as columns, each cell the newest result there with its
date and tool commit, between `<!-- exercise-ledger:begin -->` and `<!-- exercise-ledger:end -->`.
`--public FILE` writes that table with no board names and no image or build names (board types,
families and versions only) to FILE, after the privacy check: what the public repo may show.
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
LEDGER_BEGIN, LEDGER_END = "<!-- exercise-ledger:begin -->", "<!-- exercise-ledger:end -->"
PASS, FAIL, SKIP = "pass", "fail", "skip"
YES, NO, NOT_RUN, NA = "Yes", "**No**", "not run", "—"


def load(records_dir):
    """The newest record per board, in the order of their first run."""
    latest = {}
    for path in sorted(glob.glob(os.path.join(records_dir, "*.json"))):
        with open(path) as fh:
            rec = json.load(fh)
        if rec.get("v") not in (1, 2) or "board" not in rec or "steps" not in rec:
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


def _fw_label(fw_id, public):
    """A column's firmware, from the ledger's id (`family=..;image=..;version=..`): in public, the
    family and its version or class only (image and build names are the site's own)."""
    parts = dict(p.split("=", 1) for p in (fw_id or "").split(";") if "=" in p)
    if not parts:
        return "unknown firmware"
    keep = ("version", "class") if public else ("image", "class", "version", "build")
    rest = " ".join(f"{k} {parts[k]}" for k in keep if parts.get(k))
    return f"{parts.get('family', '?')} {rest}".strip()


def render_ledger(entries, public=False):
    """Verbs × (board type, firmware): the newest result of each, its date and tool commit."""
    if not entries:
        return "No ledger yet: `just exercise <layout> <board> confirm=apply --record` starts it.\n"
    cols, boards, newest = [], {}, {}
    for e in entries:
        col = (e.get("board_tag") or "?", _fw_label(e.get("fw_id"), public))
        if col not in cols:
            cols.append(col)
        boards.setdefault(col, set()).add(e.get("board") or "?")
        key = (col, e["verb"])
        if key not in newest or (e.get("ts") or "") >= (newest[key].get("ts") or ""):
            newest[key] = e
    verbs = []
    for e in entries:
        if e["verb"] not in verbs:
            verbs.append(e["verb"])
    head = [f"{tag} / {fw}" + ("" if public else f" ({', '.join(sorted(boards[(tag, fw)]))})") for tag, fw in cols]
    lines = ["| Verb | " + " | ".join(head) + " |", "|---|" + "---|" * len(cols)]
    for v in verbs:
        cells = []
        for col in cols:
            e = newest.get((col, v))
            if e is None:
                cells.append("")
            else:
                mark = YES if e["result"] == PASS else NO
                cells.append(f"{mark} {_when(e.get('ts'))[:10]} `{e.get('tool_commit') or '?'}`")
        lines.append(f"| `{v}` | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def splice(text, body, begin=BEGIN, end=END):
    """`text` with the table between the markers replaced by `body`; markers added before the
    first `## ` heading (or at the end) when the file has none."""
    block = f"{begin}\n{body}{end}"
    if begin in text and end in text:
        return re.sub(re.escape(begin) + r".*?" + re.escape(end), lambda _m: block, text, count=1, flags=re.S)
    m = re.search(r"^## ", text, re.M)
    at = m.start() if m else len(text)
    return text[:at] + block + "\n\n" + text[at:]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--records", help="the records directory (default <site>/exercise)")
    ap.add_argument("--out", help="the matrix file (default <site>/docs/CAPABILITY-MATRIX.md)")
    ap.add_argument("--check", action="store_true", help="exit 1 when the file is not what the records say")
    ap.add_argument("--stdout", action="store_true", help="print the table, write nothing")
    ap.add_argument("--ledger", action="store_true",
                    help="also the table by board type and firmware from the site's ledger")
    ap.add_argument("--public", metavar="FILE",
                    help="write the ledger table with no board, image or build names to FILE (privacy-checked)")
    a = ap.parse_args(argv)
    site.load_env()
    records_dir = a.records or os.path.join(site.site_dir(), "exercise")
    out = a.out or os.path.join(site.site_dir(), "docs", "CAPABILITY-MATRIX.md")
    if a.ledger or a.public:
        import ledger  # pylint: disable=import-outside-toplevel
        entries = ledger.read(ledger.path_of(os.path.dirname(records_dir.rstrip("/"))))
    if a.public:
        return write_public(a.public, render_ledger(entries, public=True))
    body = render(load(records_dir))
    if a.stdout:
        print(body, end="")
        if a.ledger:
            print(render_ledger(entries), end="")
        return 0
    text = ""
    if os.path.isfile(out):
        with open(out) as fh:
            text = fh.read()
    new = splice(text, body)
    if a.ledger:
        new = splice(new, render_ledger(entries), LEDGER_BEGIN, LEDGER_END)
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


def write_public(path, body):
    """The public table, refused when the privacy check finds anything of the site's in it."""
    import subprocess  # pylint: disable=import-outside-toplevel
    import tempfile  # pylint: disable=import-outside-toplevel
    text = ("# Hardware results\n\nThe newest result of each verb of the board exercise, by board type and "
            "firmware, with the date and the tool commit it ran against.\n\n" + body)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(text)
        tmp = fh.name
    try:
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "privacy_check.py"),
                            "--msg", tmp], capture_output=True, text=True, check=False)
    finally:
        os.unlink(tmp)
    if r.returncode != 0:
        print(f"{path}: not written: the privacy check refused it\n{r.stdout}{r.stderr}", file=sys.stderr)
        return 1
    with open(path, "w") as fh:
        fh.write(text)
    print(f"{path}: written (privacy check passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
