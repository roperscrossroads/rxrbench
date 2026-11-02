"""The example layouts resolve exactly as recorded in tests/golden/ (tools/golden.py).

A change to the inventory plugin, the layer engine, the families or what they publish shows
up here as a diff. When the change is meant, `just golden record --example` re-records, and the
commit says which keys changed and why.
"""
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GOLDEN = os.path.join(REPO, "tools", "golden.py")
EXAMPLES = sorted(os.listdir(os.path.join(REPO, "tests", "golden")))


@pytest.mark.plugins
@pytest.mark.parametrize("name", EXAMPLES)
def test_example_layout_resolves_as_recorded(name):
    r = subprocess.run([sys.executable, GOLDEN, "check", "--example", name],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
