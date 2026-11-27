"""The intents listing (module_utils/intents.py): parsed, planned, and the contradiction refused."""
import pytest

from ansible_collections.rxrbench.mesh.plugins.module_utils import intents

LISTING = """uart:~$ meshtastic intents
peer     set   target 0x0a0b0c0d last 0x0a0b0c0d scan on  (blepeer/central)
head     none  (mtattach/brain)
brain    none  (mtattach/want mtattach/allow)
ear      set   peer 0x0d1d2d3d  (mtear/peer)
courier  none  (mtfleet/arm)
relay    none  (mtrelay/relay)
scan     none  (mtscan/presets)
cluster  set   7 entries  (mtclus/*)
intents: 3 set
"""


def test_the_listing_is_parsed():
    r = intents.parse(LISTING)
    assert r["count"] == 3
    assert r["set"] == {"peer": "target 0x0a0b0c0d last 0x0a0b0c0d scan on",
                        "ear": "peer 0x0d1d2d3d", "cluster": "7 entries"}
    assert r["none"] == ["head", "brain", "courier", "relay", "scan"]


NEW_LISTING = """worker     none  (mtattach/brain)
hub        none  (mtattach/want mtattach/allow)
worker-rx  set   peer 0x0d1d2d3d  (mtear/peer)
intents: 1 set
"""


def test_the_listing_with_the_new_names_is_parsed():
    r = intents.parse(NEW_LISTING)
    assert r["set"] == {"worker-rx": "peer 0x0d1d2d3d"}
    assert r["none"] == ["worker", "hub"]


def test_a_node_that_remembers_nothing():
    r = intents.parse("peer     none  (blepeer/central)\nintents: 0 set\n")
    assert r["set"] == {} and r["count"] == 0
    assert intents.plan("none", r) == ([], {}, {})


def test_firmware_without_the_verb_is_an_error_not_a_clean_node():
    with pytest.raises(intents.IntentsError, match="not an intents listing"):
        intents.parse("intents: command not found")


def test_a_tally_that_disagrees_with_the_lines_is_refused():
    with pytest.raises(intents.IntentsError, match="tallies 2"):
        intents.parse("peer set x (k)\nintents: 2 set\n")


def test_the_plan_clears_when_anything_is_set():
    cmds, before, after = intents.plan("none", intents.parse(LISTING))
    assert cmds == ["meshtastic intents clear"]
    assert set(before) == {"peer", "ear", "cluster"} and after == {}


def test_only_none_is_a_declaration():
    with pytest.raises(intents.IntentsError, match="`none` is the only one"):
        intents.plan("peer", intents.parse(LISTING))


def test_a_role_declared_beside_intents_none_is_a_contradiction():
    assert intents.conflicts({"intents": "none", "brain": {"heads": {}}}) == ["brain"]
    assert intents.conflicts({"intents": "none", "hub": {"workers": {}}}) == ["hub"]
    assert intents.conflicts({"intents": "none", "worker": {"hub": "h1"}}) == ["worker"]
    assert intents.conflicts({"intents": "none", "listener": {"presets": []}, "cluster": "c1"}) == \
        ["cluster", "listener"]
    assert intents.conflicts({"intents": "none"}) == []
    assert intents.conflicts({"brain": {}}) == []
