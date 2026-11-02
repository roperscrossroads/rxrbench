"""The test library (docs/ARCHITECTURE.md#runs-tests-and-campaigns): a test lives once
and runs where a layout binds its roles; needs are checked against the bound boards; a missing
binding lists the boards that would fit and never binds by itself."""
import yaml

from ansible_collections.rxrbench.core.plugins.module_utils import testlib

TEST = {"needs": {"roles": {"a": {"families": ["zephyr"]}, "b": {"via": ["console"]}}},
        "steps": [{"board": "$a", "cmd": "kernel version", "expect": "^Zephyr"},
                  {"board": "$b", "cmd": "kernel uptime", "within": 5}]}
VIEWS = {"z1": {"family": "zephyr", "via": "console", "features": []},
         "z2": {"family": "zephyr", "via": "console", "features": ["ble"]},
         "m1": {"family": "meshtastic", "via": "steps", "features": []},
         "x1": {"family": "zephyr", "via": None, "features": []}}


def lib(tmp_path, **tests):
    d = tmp_path / "site" / "tests"
    d.mkdir(parents=True)
    for name, t in tests.items():
        (d / f"{name}.yml").write_text(yaml.safe_dump(t))
    return testlib.load(str(d))


def test_the_library_is_the_sites_the_layout_lives_in(tmp_path):
    assert testlib.library_dir(str(tmp_path / "site" / "layouts" / "t")) == str(tmp_path / "site" / "tests")


def test_one_test_bound_on_two_layouts_runs_on_each_ones_boards(tmp_path):
    tests, problems = lib(tmp_path, answer=TEST)
    assert problems == []
    one, why = testlib.bound_scenarios(tests, problems, {"answer": {"a": "z1", "b": "z2"}}, VIEWS)
    two, why2 = testlib.bound_scenarios(tests, problems, {"answer": {"a": "z2", "b": "z1"}}, VIEWS)
    assert why == why2 == []
    assert [s["board"] for s in one[0]["steps"]] == ["z1", "z2"]
    assert [s["board"] for s in two[0]["steps"]] == ["z2", "z1"]
    assert one[0]["test"] == "answer" and one[0]["bindings"] == {"a": "z1", "b": "z2"}
    assert one[0]["steps"][1]["within"] == 5, "the step is otherwise as written"


def test_an_unmet_need_is_refused_with_what_the_board_misses(tmp_path):
    tests, problems = lib(tmp_path, answer=TEST)
    _s, why = testlib.bound_scenarios(tests, problems, {"answer": {"a": "m1", "b": "m1"}}, VIEWS)
    assert "answer: role a is bound to m1, which does not fit: its family is meshtastic, the role needs zephyr" in why
    assert "answer: role b is bound to m1, which does not fit: commands reach it by steps, the role needs console" in why
    assert "answer: roles a and b are bound to one board, m1" in why
    _s, why = testlib.bound_scenarios(tests, problems, {"answer": {"a": "x1", "b": "z1"}}, VIEWS)
    assert any("nothing carries a command to it" in w for w in why)


def test_a_missing_binding_lists_the_boards_that_fit_and_binds_nothing(tmp_path):
    tests, problems = lib(tmp_path, answer=TEST)
    scenarios, why = testlib.bound_scenarios(tests, problems, {"answer": {"b": "z2"}}, VIEWS)
    assert scenarios == []
    assert why == ["answer: no board is bound to role a; boards that fit it: z1, z2 "
                   "(bench_test_bindings: {answer: {a: <board>}})"]


def test_names_the_library_does_not_have_and_clashes_are_refused(tmp_path):
    tests, problems = lib(tmp_path, answer=TEST)
    _s, why = testlib.bound_scenarios(tests, problems, {"nope": {}}, VIEWS)
    assert why == ["nope: bound, but the library has no such test (it has: answer)"]
    _s, why = testlib.bound_scenarios(tests, problems, {"answer": {"a": "z1", "b": "z2"}}, VIEWS,
                                      inline_names=["answer"])
    assert why == ["answer: an inline scenario of this layout has the same name"]
    _s, why = testlib.bound_scenarios(tests, problems, {"answer": {"a": "z1", "b": "z2", "c": "m1"}}, VIEWS)
    assert why == ["answer: binds role c, which the test does not have (roles: a, b)"]
    assert testlib.bound_scenarios(tests, problems, {}, VIEWS) == ([], [])


def test_a_tests_own_shape_is_checked(tmp_path):
    bad = {"needs": {"roles": {"a": {"family": ["zephyr"], "via": ["radio"]}, "c": {}}},
           "steps": [{"board": "z1", "cmd": "x"}, {"board": "$b", "cmd": "y"}, {"board": "$a"}]}
    tests, problems = lib(tmp_path, bad=bad)
    assert problems == [
        "bad: role a: 'family' is not a need (families, via, features)",
        "bad: role a: via ['radio']: one of console, ssh, steps",
        "bad: step 1 names board 'z1': a library test names roles ($a), a layout binds them to boards",
        "bad: step 2 names role b, which needs.roles does not declare",
        "bad: step 3 has no cmd",
        "bad: role c is declared but no step names it"]
    _s, why = testlib.bound_scenarios(tests, problems, {"bad": {"a": "z1", "c": "z2"}}, VIEWS)
    assert why == problems, "a bound test with a broken shape is refused with its problems"


def test_a_board_view_comes_from_its_vars_and_groups():
    assert testlib.board_view({"bench_family": "zephyr", "bench_via": "console"},
                              ["boards", "feature_ble", "family_zephyr"]) == \
        {"family": "zephyr", "via": "console", "features": ["ble"]}
    assert testlib.board_view({"bench_family": "meshtastic", "bench_via": None,
                               "bench_step_tasks": "meshtastic_step"}, [])["via"] == "steps"


def test_the_example_library_test_is_well_formed():
    import os
    from tests.unit.test_chip import REPO
    tests, problems = testlib.load(os.path.join(REPO, "site.example", "tests"))
    assert problems == [] and "zephyr-shells-answer" in tests
