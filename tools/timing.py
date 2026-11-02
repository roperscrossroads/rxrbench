#!/usr/bin/env python3
"""timing.py [--last N] [run-dir ...]: where the time goes (`just timing`, docs/ARCHITECTURE.md#checking-depth).

Reads the `timing-<play>.json` files of the runs named (directories under runs/, or their
names), by default the last N runs that have any (N=10). Prints, for each play, family, flasher
and route, and each task or module phase within it: how many times it ran, its mean and its
slowest time, and its time in the oldest and the newest run it appears in, so a phase that got
slower or faster shows.
"""
import glob
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def records(dirs):
    """[(run, record)] of every timing file in the given run directories, oldest run first."""
    out = []
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "timing-*.json"))):
            try:
                with open(path, encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            out.append((rec.get("run") or os.path.basename(d), rec))
    return sorted(out, key=lambda r: r[0])


def samples(recs):
    """{(play, family, flasher, route, step): [(run, seconds)]}: a step is a task, or a task's
    module phase written `task / phase`."""
    out = {}
    for run, rec in recs:
        for board in (rec.get("boards") or {}).values():
            key = (rec.get("play"), board.get("family"), board.get("flasher"), board.get("route"))
            for t in board.get("tasks") or []:
                out.setdefault(key + (t["task"],), []).append((run, t["seconds"]))
                for ph in t.get("phases") or []:
                    out.setdefault(key + (f"{t['task']} / {ph['phase']}",), []).append(
                        (run, ph["seconds"]))
    return out


def report(recs, top=25):
    rows = []
    for key, got in samples(recs).items():
        secs = [s for _, s in got]
        first = [s for r, s in got if r == got[0][0]]
        last = [s for r, s in got if r == got[-1][0]]
        rows.append((statistics.mean(secs), key, len(secs), max(secs),
                     statistics.mean(first), statistics.mean(last), got[0][0], got[-1][0]))
    rows.sort(key=lambda r: -r[0])
    lines = [f"{len(recs)} timing record(s), runs {recs[0][0]} .. {recs[-1][0]}" if recs else
             "no timing records (a play records them when given bench_run: the just recipes do)"]
    for mean, (play, fam, flasher, route, step), n, worst, first, last, r0, r1 in rows[:top]:
        trend = "" if r0 == r1 else f"  {first:.1f}s -> {last:.1f}s"
        lines.append(f"{mean:8.1f}s mean {worst:8.1f}s max  x{n:<3} {play} {fam}/{flasher}"
                     f"{'/' + route if route else ''}: {step}{trend}")
    return "\n".join(lines)


def main(argv):
    last = 10
    if argv[:1] == ["--last"]:
        last, argv = int(argv[1]), argv[2:]
    runs = os.path.join(REPO, "runs")
    if argv:
        dirs = [a if os.path.isdir(a) else os.path.join(runs, a) for a in argv]
    else:
        dirs = sorted(d for d in glob.glob(os.path.join(runs, "*"))
                      if glob.glob(os.path.join(d, "timing-*.json")))[-last:]
    print(report(records(dirs)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
