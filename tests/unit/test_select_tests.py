"""tools/select_tests.py: a change runs the tests of the features it touches, the tests that load the
file, and the plays on the boards of its plugin; shared ground, or a file nothing claims, runs all."""
import importlib.util
import os
import sys

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(REPO, "tools"))
SPEC = importlib.util.spec_from_file_location("select_tests", os.path.join(REPO, "tools", "select_tests.py"))
st = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(st)

pytestmark = pytest.mark.plugins     # it reads every bundled collection's feature index

C = "collections/ansible_collections/rxrbench"


def chosen(*paths):
    kind, why = st.select(list(paths))
    return kind, set(why)


def test_a_mesh_change_runs_the_mesh_tests_and_no_launcher_test():
    kind, got = chosen(f"{C}/mesh/plugins/module_utils/radio.py")
    assert kind == "SOME" and "tests/unit/test_radio.py" in got
    assert not [t for t in got if "launcher" in t], got


def test_a_launcher_change_runs_the_launcher_tests_and_no_mesh_test():
    kind, got = chosen(f"{C}/launcher/host/bin/launcher_serial.py")
    assert kind == "SOME"
    assert {"tests/unit/test_launcher_serial_tool.py", "tests/unit/test_launcher_apply_module.py"} <= got
    assert not [t for t in got if "mesh" in t or "meshtastic" in t or "zephyr_sim" in t], got


def test_a_core_change_also_runs_every_test_that_runs_a_play():
    kind, got = chosen(f"{C}/core/plugins/module_utils/flash/picoboot.py")
    assert kind == "SOME" and "tests/unit/test_picoboot.py" in got
    assert {"tests/sim/test_zephyr_sim.py", "tests/sim/test_nuttx_sim.py"} <= got


def test_a_plugin_change_runs_the_plays_on_its_own_boards_only():
    _, got = chosen(f"{C}/nuttx/plugins/module_utils/nfiles.py")
    assert "tests/sim/test_nuttx_sim.py" in got and "tests/sim/test_linux_sim.py" not in got


@pytest.mark.parametrize("path", ["tests/conftest.py", "tests/support/fake_launcher.py", ".github/workflows/test.yml",
                                  f"{C}/core/plugins/plugin_utils/registry.py", "requirements.txt"])
def test_shared_ground_runs_everything(path):
    assert chosen(path)[0] == "ALL"


def test_a_file_no_feature_claims_and_no_test_loads_runs_everything():
    path = "tools/" + "_".join(["never", "named", "anywhere"]) + ".py"   # built here, so no test names it
    kind, why = st.select([path])
    assert kind == "ALL" and "no feature claims it" in why[path]


def test_a_test_file_runs_itself_and_documentation_runs_the_documentation_tests():
    assert chosen("tests/unit/test_radio.py") == ("SOME", {"tests/unit/test_radio.py"})
    kind, got = chosen("docs/OVERVIEW.md")
    assert kind == "SOME" and "tests/unit/test_docs_tools.py" in got and len(got) <= len(st.DOC_TESTS)
