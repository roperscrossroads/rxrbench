"""Scenario steps on a stock Meshtastic node through its phone API (module_utils/stocksteps.py,
modules/meshtastic_stock_step.py), against a fake interface."""
import pytest

from ansible_collections.rxrbench.mesh.plugins.module_utils import stocksteps
from ansible_collections.rxrbench.mesh.plugins.modules import meshtastic_stock_step as mod


@pytest.mark.parametrize("name,num", [("0x0a0b0c0d", 0x0a0b0c0d), ("!0a0b0c0d", 0x0a0b0c0d),
                                      ("12345", 12345)])
def test_a_node_is_named_three_ways(name, num):
    assert stocksteps.node_num(name) == num


@pytest.mark.parametrize("cmd,why", [("", "empty"), ("text 0x1", "usage: text"),
                                     ("text nobody hi", "not a node"), ("nodes x", "usage: nodes"),
                                     ("reboot", "text, nodes, info and announce")])
def test_what_a_step_cannot_say(cmd, why):
    with pytest.raises(stocksteps.StepError, match=why):
        stocksteps.parse(cmd)


def test_a_text_keeps_its_words():
    assert stocksteps.parse('text !0000000a "hello there" again') == ("text", (10, "hello there again"))


def test_the_node_list_reads_like_a_zephyrtastic_console_newest_first():
    """Ages count from the newest stamp (the node's clock), whatever this host's clock says."""
    nodes = {"!a": {"num": 0xa, "lastHeard": 990, "snr": 10.0, "hopsAway": 0,
                    "user": {"longName": "alpha", "shortName": "a"}},
             "!b": {"num": 0xb, "lastHeard": 999, "snr": 11.25, "hopsAway": 1,
                    "user": {"longName": "bravo", "shortName": "b"}},
             "!c": {"num": 0xc, "user": {"longName": "never", "shortName": "c"}}}
    out = stocksteps.nodes_text(nodes, host_now=80000)
    assert out.splitlines() == [
        "nodes: 3 (node clock unknown: its own entry has no stamp; ages are from its newest, "
        "-79001 s from this host's)",
        '0x0000000b last=0s ago snr=11.2 hops=1 long="bravo" short="b"',
        '0x0000000a last=9s ago snr=10.0 hops=0 long="alpha" short="a"',
        '0x0000000c last=never snr=0.0 hops=? long="never" short="c"']


class Iface:
    def __init__(self, answer=None, me=0x99):
        self.answer, self.sent = answer, []
        self.myInfo = type("I", (), {"my_node_num": me})()
        self.nodes = {}

    def sendData(self, data, destinationId, portNum, wantAck, onResponse, onResponseAckPermitted):
        assert wantAck and onResponseAckPermitted and portNum == stocksteps.TEXT_PORT
        self.sent.append((destinationId, data))
        if self.answer is not None:
            onResponse(self.answer)


def routing(frm, reason="NONE"):
    return {"from": frm, "decoded": {"routing": {"errorReason": reason}}}


@pytest.mark.parametrize("answer,said", [
    (routing(0x0a0b0c0d), "ack from 0x0a0b0c0d"),
    (routing(0x0a0b0c0d, "NO_CHANNEL"), "nak from 0x0a0b0c0d: NO_CHANNEL"),
    (routing(0x99), "implicit ack (heard relayed, no answer from 0x0a0b0c0d)"),
    (None, "no answer within 0 s")])
def test_a_text_says_what_came_back_for_it(answer, said):
    i = Iface(answer)
    out = stocksteps.run(i, "text 0x0a0b0c0d hi", timeout=0.01)
    assert out.startswith(said) and i.sent == [(0x0a0b0c0d, b"hi")]


def test_a_step_is_asked_again_within_its_time_but_a_text_is_sent_once():
    i = Iface(routing(0x1))
    ticks = iter(range(100))
    p = {"within": 5, "timeout": 0.01}
    heard = iter([{}, {}, {"!1": {"num": 1, "lastHeard": 100, "user": {}}}])

    def sleep(_):
        i.nodes = next(heard)

    r = mod.one(i, {"name": "n", "cmd": "nodes", "expect": "0x00000001 last="}, p,
                sleep=sleep, clock=lambda: next(ticks))
    assert r["passed"]
    r = mod.one(i, {"name": "t", "cmd": "text 0x2 hi", "expect": "ack from 0x00000002"}, p,
                sleep=lambda _: pytest.fail("a text is not sent again"), clock=lambda: next(ticks))
    assert not r["passed"] and len(i.sent) == 1


def test_announce_broadcasts_its_own_user_record_on_the_nodeinfo_port():
    sent = []

    class I:
        def sendData(self, data, destinationId, portNum):
            sent.append((data, destinationId, portNum))

        def getMyNodeInfo(self):
            return {"num": 0x0a0b0c0d, "user": {}}

    assert stocksteps.announce(I(), user_bytes=b"user") == "announced 0x0a0b0c0d"
    assert sent == [(b"user", "^all", stocksteps.NODEINFO_PORT)]


def test_the_nodes_own_stamp_is_its_now_when_it_has_one():
    nodes = {"!a": {"num": 0xa, "lastHeard": 900, "user": {}},
             "!me": {"num": 0x99, "lastHeard": 1000, "user": {}}}
    lines = stocksteps.nodes_text(nodes, host_now=1000, me=0x99).splitlines()
    assert lines[0] == "nodes: 2 (node clock +0 s from this host's)"
    assert lines[2].startswith("0x0000000a last=100s ago")
