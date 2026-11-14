"""tools/features.py: the feature index covers every exercise verb, family, flasher and route the code
has; each feature's code paths and test files exist, and each has a test or says why not; a changed
file is mapped back to the features whose code it is."""
import importlib.util
import os

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
SPEC = importlib.util.spec_from_file_location("features", os.path.join(REPO, "tools", "features.py"))
features = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(features)

pytestmark = pytest.mark.plugins     # the index reads every bundled collection


def test_the_index_is_complete_and_every_path_and_test_exists():
    assert features.problems(features.load()) == []


def test_a_family_the_code_has_and_the_index_lacks_is_a_problem():
    want = features.expected()
    want["family"].add("brand-new")
    assert "the family 'brand-new' has no feature" in features.problems(features.load(), want)


def test_a_feature_with_no_test_must_say_why():
    feats = features.load()
    f = dict(next(f for f in feats if f["id"] == "core.verb.locate"))
    f["tests"] = {}
    out = features.problems([*(x for x in feats if x["id"] != f["id"]), f])
    assert "core.verb.locate: no test, and no gap saying why" in out
    f["gap"] = "not testable here"
    assert not [p for p in features.problems([*(x for x in feats if x["id"] != f["id"]), f])
                if p.startswith("core.verb.locate")]


def test_dead_paths_unknown_kinds_and_hw_verbs_are_problems():
    feats = features.load()
    core = next(f for f in feats if f["collection"] == "rxrbench.core")
    bad = {"id": "core.bad", "kind": "gadget", "summary": "x", "dir": core["dir"], "collection": core["collection"]}
    worse = {"id": "core.worse", "kind": "capability", "summary": "x", "paths": ["no/such/*.py"],
             "tests": {"unit": ["tests/unit/test_nothing_here.py"], "hw": ["fly"]},
             "dir": core["dir"], "collection": core["collection"]}
    out = features.problems([*feats, bad, worse])
    assert any(p.startswith("core.bad: kind 'gadget'") for p in out)
    assert "core.worse: path collections/ansible_collections/rxrbench/core/no/such/*.py matches nothing" in out
    assert "core.worse: test file tests/unit/test_nothing_here.py does not exist" in out
    assert "core.worse: hw verb 'fly' is not an exercise verb" in out


def test_a_changed_file_is_mapped_to_the_features_whose_code_it_is():
    feats = features.load()
    got = features.owners(feats, [
        "collections/ansible_collections/rxrbench/launcher/host/bin/launcher_serial.py",
        "collections/ansible_collections/rxrbench/mesh/plugins/module_utils/radio.py",
        "tools/exercise.py",
        "README.md"])
    assert {"launcher.app-install", "launcher.self-update"} <= set(
        got["collections/ansible_collections/rxrbench/launcher/host/bin/launcher_serial.py"])
    assert got["collections/ansible_collections/rxrbench/mesh/plugins/module_utils/radio.py"] == ["mesh.radio"]
    assert got["tools/exercise.py"] == ["core.exercise"]
    assert got["README.md"] == [], "a file no feature claims belongs to none (selection runs everything then)"
