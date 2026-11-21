"""counter_delta: two readings of a device's counters, judged by how much each moved."""
import importlib.util
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("counters", os.path.join(
    REPO, "collections", "ansible_collections", "rxrbench", "core", "plugins", "filter", "counters.py"))
counters = importlib.util.module_from_spec(spec)
spec.loader.exec_module(counters)
delta = counters.counter_delta

FRAMED = {"extract": r"~S(\{.*\})\*[0-9a-f]{4}", "path": "health", "restart": "up",
          "ignore": ["tx"], "allow": {"noise": 3}}


def framed(up, tx=0, fail=0, noise=0):
    return ('log line\n~S{"v":1,"health":{"up":%d,"tx":%d,"fail":%d,"noise":%d}}*1a2b\nprompt'
            % (up, tx, fail, noise))


def test_counters_that_did_not_move_pass_and_every_change_is_reported():
    r = delta(framed(70, tx=9), framed(10, tx=2), FRAMED)
    assert r["why"] == []
    assert r["deltas"] == {"up": 60, "tx": 7, "fail": 0, "noise": 0}


def test_an_error_counter_that_grew_fails_and_says_by_how_much():
    r = delta(framed(70, fail=2), framed(10), FRAMED)
    assert r["why"] == ["fail grew by 2 (0 -> 2)"]


def test_an_ignored_key_never_fails_and_an_allowance_is_a_ceiling():
    assert delta(framed(70, tx=500, noise=3), framed(10), FRAMED)["why"] == []
    assert delta(framed(70, noise=4), framed(10), FRAMED)["why"] == ["noise grew by 4 (0 -> 4), 3 allowed"]


def test_a_star_allowance_covers_every_key_not_named():
    s = {**FRAMED, "allow": {"*": 5}}
    assert delta(framed(70, fail=5), framed(10), s)["why"] == []
    assert delta(framed(70, fail=6), framed(10), s)["why"]


def test_a_restart_between_the_readings_fails_and_its_deltas_are_not_judged():
    r = delta(framed(5, fail=0), framed(100, fail=3), FRAMED)
    assert len(r["why"]) == 1 and "restarted" in r["why"][0]


def test_whole_output_json_with_no_extract_and_no_path():
    assert delta('{"a": 2, "b": "x"}', '{"a": 1}', {})["why"] == ["a grew by 1 (1 -> 2)"]


def test_a_key_new_in_the_second_reading_counts_from_zero():
    assert delta('{"a": 0, "n": 2}', '{"a": 0}', {})["why"] == ["n grew by 2 (0 -> 2)"]


def test_an_unreadable_reading_is_a_reason_not_an_error():
    assert "nothing that matches" in delta("garbage", framed(1), FRAMED)["why"][0]
    assert "not JSON" in delta("{nope", "{}", {})["why"][0]
    assert "no first reading" in delta(framed(1), None, FRAMED)["why"][0]


def test_optional_counters_missing_from_the_first_reading_are_not_judged():
    old = 'x\n~S{"v":1}*1a2b\n'
    r = delta(framed(70, fail=9), old, {**FRAMED, "optional": True})
    assert r["why"] == [] and r["note"].startswith("not judged")
    assert delta(framed(70, fail=9), old, FRAMED)["why"], "without optional it is a failure"


GROUPED = {"extract": r"~S(\{.*\})\*[0-9a-f]{4}", "path": "health", "restart": "up",
           "judge": "err", "record": ["ops", "warn"], "optional": True}


def grouped(up, tx=0, fail=0, warn=0):
    return ('~S{"v":1,"health":{"up":%d,"ops":{"tx":%d},"warn":{"odd":%d},"err":{"fail":%d}}}*1a2b'
            % (up, tx, warn, fail))


def test_only_the_judged_group_fails_and_the_recorded_ones_are_kept():
    r = delta(grouped(70, tx=9, warn=4), grouped(10, tx=2), GROUPED)
    assert r["why"] == [] and r["deltas"] == {"fail": 0}
    assert r["recorded"] == {"ops": {"tx": 7}, "warn": {"odd": 4}}
    assert delta(grouped(70, fail=1), grouped(10), GROUPED)["why"] == ["fail grew by 1 (0 -> 1)"]


def test_the_restart_key_is_read_beside_the_groups():
    assert "restarted" in delta(grouped(5), grouped(70), GROUPED)["why"][0]


def test_a_first_reading_without_the_judged_group_is_old_firmware():
    flat = '~S{"v":1,"health":{"up":3,"fail":0}}*1a2b'
    assert delta(grouped(9, fail=2), flat, GROUPED)["note"].startswith("not judged")
    assert delta(grouped(9, fail=2), flat, {**GROUPED, "optional": False})["why"]
