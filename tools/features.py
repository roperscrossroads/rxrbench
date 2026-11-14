#!/usr/bin/env python3
"""The tool's features, as each collection declares them, and the tests that prove each one.

Each collection keeps a `features.yml` beside its code (core knows no plugin: a plugin declares its
own). An entry:

    - id: launcher.self-update            unique, `<collection>.<name>`
      kind: capability                    verb | family | flasher | route | capability
      name: launcher                      for verb/family/flasher/route: the one it covers
      summary: Launcher updates itself     one line
      paths: [plugins/modules/launcher_apply.py, host/bin/launcher_serial.py]
                                          globs of the code it depends on, in the collection
      repo_paths: [tools/exercise.py]     globs from the repository root (a bundled collection's
                                          tools outside it)
      tests:
        unit: [tests/unit/test_launcher_apply_module.py]   files, from the repository root
        sim: []
        hw: [apply]                       exercise verbs that prove it on a board
      gap: why there is no test yet       instead of tests, never silently
      on_board: false                     a feature no board is needed for (the index, the sim
                                          bench): not listed as unproven on hardware

    tools/features.py --check     the index covers every family, exercise verb, flasher and route,
                                  every glob and test file exists, every feature has a test or a gap
    tools/features.py --gaps      the features with a gap, and those proven on no board
    tools/features.py --list      one line per feature (--json: the whole index)
    tools/features.py --owners <path>...
                                  the features whose code a path is (test selection reads this)
"""
import argparse
import fnmatch
import glob
import json
import os
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import collections_path  # noqa: E402,F401  pylint: disable=unused-import,wrong-import-position

KINDS = ("verb", "family", "flasher", "route", "capability")
TEST_KINDS = ("unit", "sim", "hw")
#: The bundled collections, core first; a site's own plugins are read from site.plugins().
BUNDLED = ("rxrbench.core", "rxrbench.mesh", "rxrbench.linux", "rxrbench.zephyr", "rxrbench.nuttx",
           "rxrbench.launcher")


def collection_dirs(names=BUNDLED):
    """{collection: its directory}, from the collections path the tool runs with."""
    from ansible_collections.rxrbench.core.plugins.plugin_utils import registry  # pylint: disable=import-outside-toplevel
    out = {}
    core = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core")
    for name in names:
        if name == "rxrbench.core":
            out[name] = core
        else:
            out.update(registry.dirs([name]))
    return out


def load(names=BUNDLED):
    """Every feature of the collections named, each with `collection` and `dir` added."""
    feats = []
    for coll, d in collection_dirs(names).items():
        p = os.path.join(d, "features.yml")
        if not os.path.isfile(p):
            continue
        with open(p) as fh:
            doc = yaml.safe_load(fh) or {}
        for f in doc.get("features") or []:
            feats.append({**f, "collection": coll, "dir": d})
    return feats


def expected():
    """What the index must cover: {kind: {name, ...}} from the code itself."""
    import exercise  # pylint: disable=import-outside-toplevel
    from ansible_collections.rxrbench.core.plugins.module_utils import routes  # pylint: disable=import-outside-toplevel
    from ansible_collections.rxrbench.core.plugins.module_utils.flash import common  # pylint: disable=import-outside-toplevel
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families  # pylint: disable=import-outside-toplevel
    return {"verb": set(exercise.VERBS), "family": set(families.specs()),
            "flasher": set(common.FLASHERS), "route": set(routes.DEFAULT_ORDER)}


def globs_of(feature):
    """A feature's globs as absolute patterns: `paths` in its collection, `repo_paths` in the repository."""
    return ([os.path.join(feature["dir"], g) for g in feature.get("paths") or []]
            + [os.path.join(REPO, g) for g in feature.get("repo_paths") or []])


def files_of(feature):
    """The repository files a feature's globs match (relative to the repository)."""
    out = set()
    for g in globs_of(feature):
        for p in glob.glob(g, recursive=True):
            if os.path.isfile(p):
                out.add(os.path.relpath(os.path.realpath(p), os.path.realpath(REPO)))
    return out


def problems(feats, want=None):
    """Every way the index falls short, as sentences."""
    want = expected() if want is None else want
    out = []
    ids = [f.get("id") for f in feats]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        out.append(f"{dup}: declared twice")
    covered = {k: set() for k in KINDS}
    import exercise  # pylint: disable=import-outside-toplevel
    verbs = set(exercise.VERBS) | {"came-back"}
    for f in feats:
        fid = f.get("id") or "?"
        if f.get("kind") not in KINDS:
            out.append(f"{fid}: kind {f.get('kind')!r} is not one of {', '.join(KINDS)}")
            continue
        if f["kind"] != "capability":
            covered[f["kind"]].add(f.get("name"))
        if not f.get("summary"):
            out.append(f"{fid}: no summary")
        if not f.get("paths") and not f.get("repo_paths"):
            out.append(f"{fid}: no paths: the code it depends on is what decides when it is tested again")
        for g in globs_of(f):
            if not glob.glob(g, recursive=True):
                out.append(f"{fid}: path {os.path.relpath(g, REPO)} matches nothing")
        tests = f.get("tests") or {}
        for k in tests:
            if k not in TEST_KINDS:
                out.append(f"{fid}: tests.{k} is not one of {', '.join(TEST_KINDS)}")
        for k in ("unit", "sim"):
            for t in tests.get(k) or []:
                if not os.path.isfile(os.path.join(REPO, t)):
                    out.append(f"{fid}: test file {t} does not exist")
        for v in tests.get("hw") or []:
            if v not in verbs:
                out.append(f"{fid}: hw verb {v!r} is not an exercise verb")
        if not any(tests.get(k) for k in TEST_KINDS) and not f.get("gap"):
            out.append(f"{fid}: no test, and no gap saying why")
    for kind, names in want.items():
        for n in sorted(names - covered[kind]):
            out.append(f"the {kind} {n!r} has no feature")
    return out


def owners(feats, paths):
    """{path: [feature ids]} for repository paths (a changed file's features)."""
    out = {}
    for path in paths:
        rel = os.path.normpath(path)
        out[path] = [f["id"] for f in feats
                     if rel in files_of(f) or any(fnmatch.fnmatch(rel, os.path.relpath(g, REPO))
                                                  for g in globs_of(f))]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--gaps", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--owners", nargs="+", metavar="PATH")
    a = ap.parse_args(argv)
    feats = load()
    if a.owners:
        print(json.dumps(owners(feats, a.owners), indent=1))
        return 0
    if a.check:
        bad = problems(feats)
        for p in bad:
            print(p)
        print(f"features: {len(feats)} declared, {len(bad)} problems")
        return 1 if bad else 0
    if a.gaps:
        for f in feats:
            t = f.get("tests") or {}
            if f.get("gap"):
                print(f"{f['id']}: gap: {f['gap']}")
            elif not t.get("hw") and f.get("on_board", True):
                print(f"{f['id']}: proven on no board (no exercise verb)")
        return 0
    if a.json:
        print(json.dumps([{k: v for k, v in f.items() if k != "dir"} for f in feats], indent=1))
        return 0
    for f in feats:
        t = f.get("tests") or {}
        n = {k: len(t.get(k) or []) for k in TEST_KINDS}
        print(f"{f['id']:<40} {f['kind']:<10} unit {n['unit']:>2} sim {n['sim']:>2} hw {n['hw']:>2}  {f.get('summary', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
