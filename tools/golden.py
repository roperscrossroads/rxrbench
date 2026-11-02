#!/usr/bin/env python3
"""golden.py record|check [--example] [layout ...]: what every layout resolves to, as files.

A refactor of the inventory plugin, the layer engine, the families or the roles must not change
what a layout means. This records, per layout, the inventory graph and, per board, the
`bench_*` variables Ansible publishes and the board's declared state with provenance
(show.py --json); `check` resolves everything again and diffs it against the record.

    just golden record            the site's layouts -> <site>/golden/   (private, the site's repo)
    just golden check             diff the site's layouts against that record
    just golden record --example  the examples -> tests/golden/ (public; tests/unit/test_golden.py)

Resolved with an empty plug-relay index (RXRBENCH_LOCATION_DIR), so what is plugged in now does
not change the record, and with the SOPS vars plugin off, so no secret is ever decrypted into
it. Machine-specific values (bench_sources, bench_layout_dir) are left out.
"""
import difflib
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402

PYTHON = os.path.join(REPO, "venv", "bin", "python")
INVENTORY = os.path.join(REPO, "venv", "bin", "ansible-inventory")
SKIP_VARS = {"bench_sources", "bench_layout_dir", "bench_secrets", "bench_families",
             "bench_site_dir", "bench_registry", "bench_facts_dir", "bench_repo"}


def _env():
    env = dict(os.environ)
    env["RXRBENCH_LOCATION_DIR"] = tempfile.mkdtemp(prefix="golden-no-location-")
    env["ANSIBLE_VARS_ENABLED"] = "host_group_vars"   # never community.sops.sops here
    return env


def _run(argv, env):
    r = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=REPO)
    if r.returncode != 0:
        raise SystemExit(f"{' '.join(argv)}\n{r.stdout}{r.stderr}")
    return r.stdout


def _boards(graph):
    """The members of @boards in `ansible-inventory --graph` output (`|  |--name` lines under
    the `|--@boards:` line, until the next group)."""
    out, inside = [], False
    for line in graph.splitlines():
        m = re.match(r"^[\s|]*\|--(@?)(.+?):?$", line)
        if not m:
            continue
        if m.group(1):
            inside = m.group(2) == "boards"
        elif inside:
            out.append(m.group(2))
    return out


def resolve(layout_dir, env):
    """{filename: text} for one layout."""
    name = os.path.basename(layout_dir)
    graph = _run([INVENTORY, "-i", layout_dir, "--graph"], env)
    out = {"graph.txt": graph}
    for board in _boards(graph):
        host = json.loads(_run([INVENTORY, "-i", layout_dir, "--host", board], env))
        keep = {k: v for k, v in sorted(host.items()) if k.startswith("bench_") and k not in SKIP_VARS}
        out[f"{board}.vars.json"] = json.dumps(keep, indent=1, sort_keys=True, default=str) + "\n"
        shown = _run([PYTHON, os.path.join(REPO, "tools", "show.py"), name, board, "--json"], env)
        out[f"{board}.show.json"] = json.dumps(json.loads(shown), indent=1, sort_keys=True) + "\n"
    return out


def layouts(example, names):
    base = os.path.join(site.EXAMPLE if example else site.site_dir(), "layouts")
    dirs = sorted(os.path.dirname(p) for p in glob.glob(os.path.join(base, "*", "devreg.yml")))
    if names:
        dirs = [d for d in dirs if os.path.basename(d) in names]
        missing = set(names) - {os.path.basename(d) for d in dirs}
        if missing:
            raise SystemExit(f"no such layout under {base}: {', '.join(sorted(missing))}")
    return dirs


def golden_dir(example):
    return os.path.join(REPO, "tests", "golden") if example else os.path.join(site.site_dir(), "golden")


def record(example, names):
    root = golden_dir(example)
    env = _env()
    for d in layouts(example, names):
        target = os.path.join(root, os.path.basename(d))
        os.makedirs(target, exist_ok=True)
        files = resolve(d, env)
        for stale in os.listdir(target):
            if stale not in files:
                os.remove(os.path.join(target, stale))
        for fname, text in files.items():
            with open(os.path.join(target, fname), "w") as fh:
                fh.write(text)
        print(f"recorded {os.path.basename(d)}: {len(files)} files -> {target}")


def check(example, names):
    """Returns the list of differences (empty = clean); prints each as a unified diff."""
    root = golden_dir(example)
    env = _env()
    problems = []
    for d in layouts(example, names):
        name = os.path.basename(d)
        target = os.path.join(root, name)
        if not os.path.isdir(target):
            problems.append(f"{name}: no record at {target} (just golden record)")
            continue
        files = resolve(d, env)
        for fname in sorted(set(files) | set(os.listdir(target))):
            want = files.get(fname)
            path = os.path.join(target, fname)
            have = open(path).read() if os.path.exists(path) else None
            if want == have:
                continue
            problems.append(f"{name}/{fname}")
            diff = difflib.unified_diff((have or "").splitlines(True), (want or "").splitlines(True),
                                        fromfile=f"recorded/{name}/{fname}", tofile=f"now/{name}/{fname}")
            sys.stdout.writelines(diff)
    return problems


def main(argv):
    if not argv or argv[0] not in ("record", "check"):
        raise SystemExit(__doc__)
    example = "--example" in argv
    names = [a for a in argv[1:] if a != "--example"]
    site.load_env()
    if argv[0] == "record":
        record(example, names)
        return 0
    problems = check(example, names)
    if problems:
        print(f"golden: {len(problems)} difference(s): " + ", ".join(problems))
        return 1
    print("golden: every layout resolves as recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
