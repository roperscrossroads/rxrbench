import pytest

from ansible_collections.rxrbench.mesh.plugins.module_utils import attach, state

IDS = {"xiao1": "0x0a1a2a3a", "xiao2": "0x0b1b2b3b"}


def brain(*heads):
    return {"max_heads": 4, "heads": [
        {"attachment": a, "node_id": n, "link_up": up, "preset": p, "wanted_preset": w}
        for a, n, p, w, up in heads]}


RIG_A = brain((1, "0x0a1a2a3a", "MEDIUM_FAST", "MEDIUM_FAST", True),
              (2, "0x0b1b2b3b", "SHORT_FAST", "SHORT_FAST", True))
DECLARED = {"xiao1": "MEDIUM_FAST", "xiao2": "SHORT_FAST"}


def test_a_brain_as_declared_gets_no_command():
    assert attach.plan_heads(DECLARED, IDS, RIG_A) == ([], {})
    assert attach.heads_left(DECLARED, IDS, RIG_A) == {}


def test_a_head_with_no_wish_is_named_by_its_attachment_and_the_preset_by_number():
    b = brain((1, "0x0a1a2a3a", "MEDIUM_FAST", None, True),
              (2, "0x0b1b2b3b", "MEDIUM_FAST", "MEDIUM_FAST", True))
    cmds, delta = attach.plan_heads(DECLARED, IDS, b)
    # attachment 2 is xiao2 (matched by node id, not by order); SHORT_FAST is 6, MEDIUM_FAST 4
    assert cmds == ["meshtastic attach preset 1 4", "meshtastic attach preset 2 6"]
    assert delta == {"xiao1": (None, "MEDIUM_FAST"), "xiao2": ("MEDIUM_FAST", "SHORT_FAST")}


def test_node_ids_compare_whatever_their_spelling():
    ids = {"xiao1": "0A1A2A3A", "xiao2": "0xb1b2b3b"}
    assert attach.plan_heads(DECLARED, ids, RIG_A) == ([], {})


def test_a_declared_head_that_is_not_attached_is_an_error_not_a_skip():
    b = brain((1, "0x0a1a2a3a", "MEDIUM_FAST", "MEDIUM_FAST", True))
    with pytest.raises(attach.AttachError, match=r"not attached to this brain: xiao2 \(0x0b1b2b3b\)"):
        attach.plan_heads(DECLARED, IDS, b)


def test_an_attached_head_the_layout_does_not_name_is_left_alone():
    assert attach.plan_heads({"xiao1": "MEDIUM_FAST"}, IDS, RIG_A) == ([], {})


def test_a_head_still_moving_is_left_but_one_with_its_link_down_is_not_waited_for():
    moving = brain((1, "0x0a1a2a3a", "LONG_FAST", "MEDIUM_FAST", True),
                   (2, "0x0b1b2b3b", "LONG_FAST", "SHORT_FAST", False))
    assert attach.heads_left(DECLARED, IDS, moving) == {
        "xiao1": "wanted MEDIUM_FAST but still on LONG_FAST"}
    assert attach.heads_left(DECLARED, IDS, moving, retuned=False) == {}


def test_an_unknown_preset_is_refused_before_anything_is_sent():
    with pytest.raises(attach.AttachError, match="not a preset"):
        attach.plan_heads({"xiao1": "MediumFast"}, IDS, RIG_A)


def test_preset_numbers_are_the_firmwares():
    assert attach.PRESET_NUMBER["SHORT_FAST"] == 6
    assert attach.PRESET_NUMBER["MEDIUM_FAST"] == 4


def test_a_listener_as_declared_gets_no_command():
    assert attach.plan_scan(["MEDIUM_FAST"], {"presets": ["MEDIUM_FAST"]}) == ([], None)


def test_a_listener_is_given_the_whole_list_by_display_name_in_order():
    cmds, delta = attach.plan_scan(["MEDIUM_FAST", "SHORT_TURBO"],
                                   {"presets": ["SHORT_TURBO", "MEDIUM_FAST"]})
    assert cmds == ["meshtastic scan presets MediumFast ShortTurbo"]
    assert delta == (["SHORT_TURBO", "MEDIUM_FAST"], ["MEDIUM_FAST", "SHORT_TURBO"])


@pytest.mark.parametrize("bad, why", [
    ([], "empty"),
    (["MediumFast"], "cannot be swept"),
    (["VERY_LONG_SLOW"], "cannot be swept"),
    (["MEDIUM_FAST", "MEDIUM_FAST"], "twice"),
])
def test_a_list_the_shell_cannot_take_is_refused(bad, why):
    with pytest.raises(attach.AttachError, match=why):
        attach.plan_scan(bad, {"presets": []})


def test_the_state_line_of_a_brain_and_of_a_listener_reads_as_the_plans_expect():
    import binascii
    import json

    def line(j):
        t = json.dumps(j, separators=(",", ":"))
        return f"~S{t}*{binascii.crc_hqx(t.encode(), 0):04x}\n"

    base = {"v": 1, "build": "x", "board": "b", "class": 6, "id": "0x0c1c2c3c"}
    f = state.state_facts(line({**base, "brain": {"max": 4, "heads": [
        {"a": 1, "node": "0x0a1a2a3a", "p": 4, "w": 4, "up": 1},
        {"a": 2, "node": "0x0b1b2b3b", "p": 6, "w": 6, "up": 1}]}}))
    assert attach.plan_heads(DECLARED, IDS, f["brain"]) == ([], {})
    f = state.state_facts(line({**base, "scan": {"shut": 1, "sweep": 1, "presets": [4]}}))
    assert attach.plan_scan(["MEDIUM_FAST"], f["scanner"]) == ([], None)
