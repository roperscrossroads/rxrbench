#!/usr/bin/env python3
"""The tests a change needs, from what it changed: not every test, every time.

    tools/select_tests.py [BASE[..HEAD]]     the test files for the changes between two commits
                                             (default: origin/main..HEAD), one per line, or ALL
    tools/select_tests.py --files a b ...    the same for a list of changed files
    --why                                    each test with the changes that brought it in (JSON)

How a changed file chooses tests, most conservative first:

- shared ground (the test harness, core's plugin loading, requirements, the workflows, the
  justfile, a file nothing below claims): ALL;
- a test file: itself;
- documentation: the documentation tests;
- code a feature claims (features.yml paths): that feature's unit and sim tests, and every test
  that loads the file by import or by name; a core file also brings in every test that runs a
  playbook (core is under every play); a plugin's file, the playbook tests that use that plugin
  (import it, or name its collection or one of its families).

Pushes to main and the nightly run test everything; this is for pull requests and local runs.
"""
import argparse
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import features  # noqa: E402  pylint: disable=wrong-import-position

C = "collections/ansible_collections/rxrbench"
#: Changes that can move any test: everything runs.
SHARED = ("tests/conftest.py", "tests/__init__.py", "tests/support/**", "tests/fixtures/**", "tests/golden/**",
          f"{C}/core/plugins/plugin_utils/**", f"{C}/core/plugins/module_utils/familyspec.py",
          f"{C}/core/plugins/module_utils/site.py", f"{C}/core/plugins/module_utils/flash/common.py",
          f"{C}/core/roles/board/**", f"{C}/*/galaxy.yml", f"{C}/*/meta/**", "requirements*", "*/requirements*",
          "ansible.cfg", "justfile", "*.just", "*/*.just", ".github/**", ".forgejo/**", "tools/collections_path.py",
          "site.example/**")
DOCS = ("*.md", "docs/**", f"{C}/*/README.md", f"{C}/*/CHANGELOG.md")
DOC_TESTS = ("tests/unit/test_docs_tools.py", "tests/unit/test_hygiene.py", "tests/unit/test_module_docs.py",
             "tests/unit/test_terms.py")
PLAYBOOK = re.compile(r"ansible-playbook|ansible_playbook")


def _match(path, patterns):
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def changed(rev):
    base, _, head = rev.partition("..")
    out = subprocess.run(["git", "diff", "--name-only", base, head or "HEAD"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def test_files():
    """{test file: its text} for every unit and sim test file."""
    out = {}
    for p in sorted(glob.glob(os.path.join(REPO, "tests", "unit", "test_*.py"))
                    + glob.glob(os.path.join(REPO, "tests", "sim", "test_*.py"))):
        with open(p) as fh:
            out[os.path.relpath(p, REPO)] = fh.read()
    return out


def module_names(path):
    """How a test can name a file: its dotted import path and its file name."""
    names = {os.path.basename(path)}
    if path.endswith(".py") and path.startswith("collections/"):
        names.add(path[len("collections/"):-3].replace("/", "."))
    return names


def plugin_of(path):
    m = re.match(rf"{re.escape(C)}/([^/]+)/", path)
    return m.group(1) if m else None


def plugin_words(feats):
    """{plugin: words a test that uses it would contain}: its collection name and its families."""
    out = {}
    for f in feats:
        p = f["collection"].split(".", 1)[1]
        w = out.setdefault(p, {f["collection"], f"rxrbench/{p}/"})
        if f["kind"] == "family":
            w.add(f["name"])
    return out


def select(paths, feats=None, tests=None):
    """(ALL or {test file: [changes that bring it in]})."""
    feats = features.load() if feats is None else feats
    tests = test_files() if tests is None else tests
    why = {}
    playbook_tests = {t for t, text in tests.items() if PLAYBOOK.search(text)}
    words = plugin_words(feats)
    owners = features.owners(feats, paths)
    by_id = {f["id"]: f for f in feats}

    def add(t, reason):
        if t in tests:
            why.setdefault(t, []).append(reason)

    for path in paths:
        if _match(path, SHARED):
            return "ALL", {path: "shared ground"}
        if path.startswith("tests/"):
            if path in tests:
                add(path, path)
            elif os.path.exists(os.path.join(REPO, path)):
                return "ALL", {path: "a test file outside unit and sim (support, data)"}
            continue                                   # a test file removed
        if _match(path, DOCS):
            for t in DOC_TESTS:
                add(t, path)
            continue
        ids = owners.get(path) or []
        names = module_names(path)
        loaders = [t for t, text in tests.items() if any(n in text for n in names)]
        if not ids and not loaders:
            return "ALL", {path: "no feature claims it and no test loads it"}
        for fid in ids:
            for kind in ("unit", "sim"):
                for t in (by_id[fid].get("tests") or {}).get(kind) or []:
                    add(t, f"{path} ({fid})")
        for t in loaders:
            add(t, f"{path} (loaded by name)")
        plugin = plugin_of(path)
        if plugin == "core" or path.startswith("tools/"):
            for t in playbook_tests:
                add(t, f"{path} (core: under every play)")
        elif plugin:
            for t in playbook_tests:
                if any(w in tests[t] for w in words.get(plugin, ())):
                    add(t, f"{path} (a play on a {plugin} board)")
    return "SOME", why


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rev", nargs="?", default="origin/main..HEAD")
    ap.add_argument("--files", nargs="+")
    ap.add_argument("--why", action="store_true")
    a = ap.parse_args(argv)
    paths = a.files or changed(a.rev)
    kind, why = select(paths)
    if a.why:
        print(json.dumps({"select": kind, "why": why}, indent=1))
    elif kind == "ALL":
        print("ALL")
    else:
        print("\n".join(sorted(why)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
