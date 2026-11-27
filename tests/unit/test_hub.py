import pytest

from ansible_collections.rxrbench.mesh.plugins.module_utils import hub, state

IDS = {"xiao1": "0x0a1a2a3a", "xiao2": "0x0b1b2b3b"}


def hub_facts(*workers):
    return {"max_workers": 4, "workers": [
        {"number": a, "node_id": n, "link_up": up, "preset": p, "wanted_preset": w}
        for a, n, p, w, up in workers]}


SETUP_A = hub_facts((1, "0x0a1a2a3a", "MEDIUM_FAST", "MEDIUM_FAST", True),
              (2, "0x0b1b2b3b", "SHORT_FAST", "SHORT_FAST", True))
DECLARED = {"xiao1": "MEDIUM_FAST", "xiao2": "SHORT_FAST"}


def test_a_hub_as_declared_gets_no_command():
    assert hub.plan_workers(DECLARED, IDS, SETUP_A) == ([], {})
    assert hub.workers_left(DECLARED, IDS, SETUP_A) == {}


def test_a_worker_with_no_wish_is_named_by_its_number_and_the_preset_by_number():
    b = hub_facts((1, "0x0a1a2a3a", "MEDIUM_FAST", None, True),
              (2, "0x0b1b2b3b", "MEDIUM_FAST", "MEDIUM_FAST", True))
    cmds, delta = hub.plan_workers(DECLARED, IDS, b)
    # worker 2 is xiao2 (matched by node id, not by order); SHORT_FAST is 6, MEDIUM_FAST 4
    assert cmds == ["meshtastic hub preset 1 4", "meshtastic hub preset 2 6"]
    assert delta == {"xiao1": (None, "MEDIUM_FAST"), "xiao2": ("MEDIUM_FAST", "SHORT_FAST")}


def test_node_ids_compare_whatever_their_spelling():
    ids = {"xiao1": "0A1A2A3A", "xiao2": "0xb1b2b3b"}
    assert hub.plan_workers(DECLARED, ids, SETUP_A) == ([], {})


def test_a_declared_worker_that_is_not_linked_is_an_error_not_a_skip():
    b = hub_facts((1, "0x0a1a2a3a", "MEDIUM_FAST", "MEDIUM_FAST", True))
    with pytest.raises(hub.HubError, match=r"not linked to this hub: xiao2 \(0x0b1b2b3b\)"):
        hub.plan_workers(DECLARED, IDS, b)


def test_a_linked_worker_the_layout_does_not_name_is_left_alone():
    assert hub.plan_workers({"xiao1": "MEDIUM_FAST"}, IDS, SETUP_A) == ([], {})


def test_a_worker_still_moving_is_left_but_one_with_its_link_down_is_not_waited_for():
    moving = hub_facts((1, "0x0a1a2a3a", "LONG_FAST", "MEDIUM_FAST", True),
                   (2, "0x0b1b2b3b", "LONG_FAST", "SHORT_FAST", False))
    assert hub.workers_left(DECLARED, IDS, moving) == {
        "xiao1": "wanted MEDIUM_FAST but still on LONG_FAST"}
    assert hub.workers_left(DECLARED, IDS, moving, retuned=False) == {}


def test_an_unknown_preset_is_refused_before_anything_is_sent():
    with pytest.raises(hub.HubError, match="not a preset"):
        hub.plan_workers({"xiao1": "MediumFast"}, IDS, SETUP_A)


def test_preset_numbers_are_the_firmwares():
    assert hub.PRESET_NUMBER["SHORT_FAST"] == 6
    assert hub.PRESET_NUMBER["MEDIUM_FAST"] == 4


def test_a_listener_as_declared_gets_no_command():
    assert hub.plan_scan(["MEDIUM_FAST"], {"presets": ["MEDIUM_FAST"]}) == ([], None)


def test_a_listener_is_given_the_whole_list_by_display_name_in_order():
    cmds, delta = hub.plan_scan(["MEDIUM_FAST", "SHORT_TURBO"],
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
    with pytest.raises(hub.HubError, match=why):
        hub.plan_scan(bad, {"presets": []})


def test_the_state_line_of_a_hub_old_or_new_and_of_a_listener_reads_as_the_plans_expect():
    import binascii
    import json

    def line(j):
        t = json.dumps(j, separators=(",", ":"))
        return f"~S{t}*{binascii.crc_hqx(t.encode(), 0):04x}\n"

    base = {"v": 1, "build": "x", "board": "b", "class": 6, "id": "0x0c1c2c3c"}
    workers = [{"a": 1, "node": "0x0a1a2a3a", "p": 4, "w": 4, "up": 1},
               {"a": 2, "node": "0x0b1b2b3b", "p": 6, "w": 6, "up": 1}]
    new = state.state_facts(line({**base, "hub": {"max": 4, "workers": workers}}))
    old = state.state_facts(line({**base, "brain": {"max": 4, "heads": workers}}))
    assert new["hub"] == old["hub"]
    assert hub.plan_workers(DECLARED, IDS, new["hub"]) == ([], {})
    assert state.state_facts(line({**base, "worker": {"hub": "0x0c1c2c3c"}}))["worker"] == \
        state.state_facts(line({**base, "head": {"brain": "0x0c1c2c3c"}}))["worker"] == \
        {"hub": "0x0c1c2c3c"}
    f = state.state_facts(line({**base, "scan": {"shut": 1, "sweep": 1, "presets": [4]}}))
    assert hub.plan_scan(["MEDIUM_FAST"], f["scanner"]) == ([], None)


def test_a_layout_block_reads_the_same_in_its_old_spelling():
    assert hub.declared_workers({"hub": {"workers": {"w1": "MEDIUM_FAST"}}}) == \
        hub.declared_workers({"brain": {"heads": {"w1": "MEDIUM_FAST"}}}) == {"w1": "MEDIUM_FAST"}
    assert hub.declared_workers({"listener": {}}) is None
    assert hub.declared_hub({"worker": {"hub": "h1"}}) == hub.declared_hub({"head": {"brain": "h1"}}) == "h1"
    assert hub.is_worker({"head": {"brain": "h1"}}) and hub.is_worker({"worker": {"hub": "h1"}})
    assert not hub.is_worker({"hub": {"workers": {}}})


class FakeBoard:
    def __init__(self, knows_new):
        self.knows_new, self.sent = knows_new, []

    def exec(self, cmd, deadline=None):
        self.sent.append(cmd)
        if cmd.startswith("meshtastic hub ") and not self.knows_new:
            return "hub: command not found"
        return "ok"


def test_the_new_verb_is_sent_first_and_the_old_one_only_to_firmware_without_it():
    new = FakeBoard(knows_new=True)
    assert hub.run(new, "meshtastic hub preset 1 4", 5) == "ok"
    assert new.sent == ["meshtastic hub preset 1 4"]
    old = FakeBoard(knows_new=False)
    assert hub.run(old, "meshtastic hub preset 1 4", 5) == "ok"
    assert old.sent == ["meshtastic hub preset 1 4", "meshtastic attach preset 1 4"]
