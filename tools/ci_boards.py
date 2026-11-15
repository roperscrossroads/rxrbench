#!/usr/bin/env python3
"""ci_boards.py <layout> [--json]: the boards of a layout a CI job may exercise, those it marks
`bench_ci: {run: true}`, each with whether its destructive verbs may run (`destructive: true`).
A board with no `bench_ci`, or `run: false`, is not listed: CI never touches it. A malformed
`bench_ci` is an error, not a board left out quietly.

`--json` prints `[{"board": ..., "destructive": ...}]`, the matrix a workflow runs
`tools/exercise.py <layout> <board> confirm=apply --ci` over.
"""
import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exercise  # noqa: E402  pylint: disable=wrong-import-position
from exercise import site  # noqa: E402  pylint: disable=wrong-import-position


def boards(inventory):
    """[(board, destructive)] from `ansible-inventory --list` output; raises ValueError on a
    malformed bench_ci."""
    out = []
    for board, hv in sorted((inventory.get("_meta") or {}).get("hostvars", {}).items()):
        if "bench_ci" not in hv:
            continue
        run, destructive, why = exercise.ci_policy(hv)
        if not run and why.startswith("bench_ci must be"):
            raise ValueError(f"{board}: {why}")
        if run:
            out.append((board, destructive))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("layout")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    site.load_env()
    inv = subprocess.run([os.path.join(exercise.VENV, "ansible-inventory"), "-i", site.layout_path(a.layout),
                          "--list"], cwd=REPO, capture_output=True, text=True, check=False)
    if inv.returncode:
        sys.exit(f"ci_boards: {inv.stderr.strip() or inv.stdout.strip()}")
    try:
        found = boards(json.loads(inv.stdout))
    except ValueError as e:
        sys.exit(f"ci_boards: {e}")
    if a.json:
        print(json.dumps([{"board": b, "destructive": d} for b, d in found]))
    else:
        for b, d in found:
            print(f"{b}\t{'destructive' if d else 'non-destructive'}")
        if not found:
            print(f"ci_boards: no board of {a.layout} is marked bench_ci: {{run: true}}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
