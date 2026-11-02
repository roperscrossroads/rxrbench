#!/usr/bin/env python3
"""Commit what a play wrote to the facts directory, as one `chore(facts)` commit, when asked.

    facts_commit.py --facts-dir <facts> --message "survey on bench1, 12 boards"

Facts are generated (`just survey`, the bootloader a boot run saw, what an apply installed into
a slot) and a site may keep them under git beside its registry, where every run then leaves the
tree dirty. This commits them in one batch, and only when the site opted in: `RXRBENCH_FACTS_COMMIT=1`
in `.env`. Without it, or when the facts directory is not in a git work tree, nothing happens
and one line says so. When the work tree holds modified tracked files outside the facts
directory they are named and nothing is committed: a batch of facts never sweeps a half-edited
registry along. Untracked files outside it (a note in progress, a scratch log) are not in the
way: a commit of the facts path alone never picks them up.

Exit 0 in every case but a git failure: a facts commit is housekeeping, never the reason a play
fails.
"""
import argparse
import os
import subprocess
import sys


def git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)
    return r.returncode, (r.stdout + r.stderr).strip()


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--facts-dir", required=True)
    ap.add_argument("--message", required=True, help="what wrote the facts: `<play> on <host>, <n> boards`")
    a = ap.parse_args(argv)
    facts = os.path.realpath(os.path.expanduser(a.facts_dir))
    if not truthy(os.environ.get("RXRBENCH_FACTS_COMMIT")):
        print("facts: not committed (RXRBENCH_FACTS_COMMIT is not set)")
        return 0
    if not os.path.isdir(facts):
        print(f"facts: {facts} is not a directory")
        return 0
    rc, top = git(facts, "rev-parse", "--show-toplevel")
    if rc != 0:
        print(f"facts: {facts} is not in a git work tree: not committed")
        return 0
    top = os.path.realpath(top)
    rel = os.path.relpath(facts, top)
    r = subprocess.run(["git", "-C", top, "status", "--porcelain", "--untracked-files=all"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        print(f"facts: git status failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    # porcelain v1: two status columns, a space, the path (a rename shows `old -> new`)
    entries = [(ln[:2], ln[3:].split(" -> ")[-1].strip('"')) for ln in r.stdout.splitlines() if len(ln) > 3]
    within = lambda p: p == rel or p.startswith(rel.rstrip("/") + "/")  # noqa: E731
    inside = [p for _, p in entries if within(p)]
    outside = [p for code, p in entries if not within(p) and code != "??"]
    if not inside:
        print("facts: unchanged")
        return 0
    if outside:
        shown = ", ".join(outside[:5]) + (", ..." if len(outside) > 5 else "")
        print(f"facts: {len(inside)} changed, left uncommitted: the work tree holds modified files ({shown})")
        return 0
    rc, out = git(top, "add", "--", rel)
    if rc != 0:
        print(f"facts: git add failed: {out}", file=sys.stderr)
        return 1
    rc, out = git(top, "commit", "-q", "-m", f"chore(facts): {a.message}")
    if rc != 0:
        print(f"facts: git commit failed: {out}", file=sys.stderr)
        return 1
    rc, sha = git(top, "rev-parse", "--short", "HEAD")
    print(f"facts: committed {len(inside)} file(s) as {sha} (chore(facts): {a.message})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
