"""Counter readings around a scenario: which scenarios get them, on which boards, judged how."""
from ansible_collections.rxrbench.core.plugins.module_utils import readings

SPEC = {"cmd": "counters", "path": "c", "judge": "err", "allow": {"noise": 2}, "ignore": ["gauge"]}


def scenario(**kw):
    return {"name": "s", "steps": [{"board": "a", "cmd": "send"}, {"board": "b", "cmd": "hear"},
                                   {"board": "a", "cmd": "again"}], **kw}


def boards(**extra):
    return {"a": {"bench_health_counters": SPEC, **extra}, "b": {"bench_health_counters": None}}


def test_a_scenario_that_does_not_ask_is_left_as_it_was():
    sc = scenario()
    assert readings.with_counters(sc, boards()) is sc
    assert readings.with_counters(scenario(counters=False), boards(), default=True)["steps"] == sc["steps"]


def test_each_board_with_counters_is_read_once_before_and_once_after_always():
    out = readings.with_counters(scenario(counters=True), boards())["steps"]
    assert [s.get("name") for s in out] == ["a counters before", None, None, None, "a counters after"]
    before, after = out[0], out[-1]
    assert before == {"name": "a counters before", "board": "a", "cmd": "counters", "keep": "counters"}
    assert after["always"] is True and after["delta"]["against"] == "counters"
    assert "cmd" not in after["delta"] and after["delta"]["judge"] == "err"


def test_the_layout_default_asks_for_every_scenario():
    assert len(readings.with_counters(scenario(), boards(), default=True)["steps"]) == 5


def test_a_board_with_no_counters_or_unknown_is_not_read():
    sc = scenario(counters=True)
    sc["steps"].append({"board": "ghost", "cmd": "x"})
    assert [s["board"] for s in readings.with_counters(sc, boards())["steps"]] == ["a", "a", "b", "a", "ghost", "a"]
    assert readings.with_counters(scenario(counters=True), {})["steps"] == scenario()["steps"]


def test_allowances_merge_family_then_board_then_scenario():
    out = readings.with_counters(
        scenario(counters={"allow": {"noise": 9, "late": 1}, "ignore": ["extra"]}),
        boards(bench_counters_allow={"noise": 5, "drop": 3}, bench_counters_ignore=["fresh"]))
    d = out["steps"][-1]["delta"]
    assert d["allow"] == {"noise": 9, "drop": 3, "late": 1}
    assert d["ignore"] == ["gauge", "fresh", "extra"]
    assert SPEC["allow"] == {"noise": 2} and SPEC["ignore"] == ["gauge"], "the family's spec is not changed"
