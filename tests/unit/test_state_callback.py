"""The state record's per-board fields (docs/ARCHITECTURE.md#runs-tests-and-campaigns): the declared image when the
layout pins bench_image, the staged image, the chip findings and the depth, from task results."""
from types import SimpleNamespace

from ansible_collections.rxrbench.core.plugins.callback import state


def result(host, res):
    return SimpleNamespace(_host=SimpleNamespace(get_name=lambda: host), _result=res)


def test_the_declared_image_is_named_when_the_layout_pins_bench_image():
    assert state.declared({"family": "zephyrtastic", "class": 6}, "/srv/bench/store/zt/class-6-abc/") == \
        {"family": "zephyrtastic", "class": 6, "image": "class-6-abc", "image_from": "bench_image"}
    assert state.declared({"family": "bare", "image": "zephyr-shell"}, "/s/zephyr-shell-b") == \
        {"family": "bare", "image": "zephyr-shell"}, "firmware's own image stands"
    assert state.declared(None, None) == {}


def test_task_results_fill_a_boards_record():
    cb = state.CallbackModule()
    cb.v2_runner_on_ok(result("b1", {"ansible_facts": {"board_image": "/s/img-a"}}))
    cb.v2_runner_on_ok(result("b1", {"ansible_facts": {"board_chip_findings": ["wifite at 0x260000 holds other firmware"]}}))
    cb.v2_runner_on_ok(result("b1", {"changed": True, "device": {"family": "bare", "hash": "ab"},
                                     "checked": {"depth": "full", "reason": "asked"},
                                     "identity_checked": {"depth": "spot", "reason": "the last full read"}}))
    b = cb._boards["b1"]
    assert b["staged"] == "/s/img-a" and b["foreign"] == ["wifite at 0x260000 holds other firmware"]
    assert b["changes"] == 1 and b["device"] == {"family": "bare", "hash": "ab"}
    assert b["checked"] == {"depth": "full", "reason": "asked"}
    assert b["identity_checked"] == {"depth": "spot", "reason": "the last full read"}
