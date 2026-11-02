#!/usr/bin/env python3
"""Core without the bundled plugins (core-portability P1, docs/ARCHITECTURE.md#core-portability).

Builds a collections tree that holds `rxrbench.core` and the fixture plugin
(`tests/fixtures/.../fixture/widgets`) and nothing else, then runs the unit tests against it:

    ./venv/bin/python tools/core_only.py [pytest args ...]      # `just core-only`

A test file that imports a bundled plugin is that plugin's test and is not collected; a test
of core that uses a bundled plugin's family or board type as its data says so with
`@pytest.mark.plugins` and is deselected. Every other test must pass: a failure is core needing
a plugin, which is fixed in core (a Family hook, catalog data, a clean refusal), never by
marking the test.
"""
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREE = os.path.join(REPO, ".build", "core-only", "collections")
LINKS = {
    ("rxrbench", "core"): os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core"),
    ("fixture", "widgets"): os.path.join(REPO, "tests", "fixtures", "collections",
                                         "ansible_collections", "fixture", "widgets"),
}


def build():
    shutil.rmtree(os.path.dirname(TREE), ignore_errors=True)
    for (ns, name), target in LINKS.items():
        d = os.path.join(TREE, "ansible_collections", ns)
        os.makedirs(d, exist_ok=True)
        os.symlink(target, os.path.join(d, name))
    return TREE


def env(tree):
    return {**os.environ, "RXRBENCH_COLLECTIONS": tree, "ANSIBLE_COLLECTIONS_PATH": tree,
            "RXRBENCH_PLUGINS": "fixture.widgets", "RXRBENCH_CORE_ONLY": "1"}


def main(argv):
    tree = build()
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "not plugins",
           os.path.join(REPO, "tests", "unit"), *argv]
    return subprocess.call(cmd, cwd=REPO, env=env(tree))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
