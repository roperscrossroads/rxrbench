"""Identity from a record (docs/ARCHITECTURE.md#checking-depth): in a plan at spot, a board's last full read
stands in for asking it, only while the plug watcher shows nothing on its port since."""
import json
import os
import time

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import identity, lastread
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common

MANIFEST = {"family": "bare", "hash": "b" * 64}
BUSID = "1-1.3"


class Fake(common.Flasher):
    name = "b1"
    spot_reads = ("identity",)

    def __init__(self, runs):
        self.runs, self.asked = runs, 0

    def device(self):
        self.asked += 1
        if self.runs is None:
            raise common.NotOurs("b1 does not answer as bare")
        return identity.make("bare", hash=self.runs)

    def write(self):
        self.runs = MANIFEST["hash"]

    def verify(self):
        return self.device()


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A bench host's state directory, with a plug watcher that started an hour ago and runs
    still (this test's own pid stands in for it)."""
    monkeypatch.setenv("RXRBENCH_HOST_STATE", str(tmp_path))
    monkeypatch.setattr(lastread, "_alive", lambda pid: True)
    since = time.time() - 3600
    (tmp_path / "usb-present.json").write_text(json.dumps(
        {"pid": os.getpid(), "watching_since": stamp(since),
         "devices": [{"busid": BUSID, "serial": "S1"}]}))
    (tmp_path / "plug-events.jsonl").write_text(
        json.dumps({"t": stamp(since), "action": "start"}) + "\n")
    return tmp_path


def event(host, action, busid=BUSID, serial="S1", t=None):
    with open(host / "plug-events.jsonl", "a") as f:
        f.write(json.dumps({"t": stamp(t or time.time() + 1), "action": action, "busid": busid,
                            "serial": serial}) + "\n")


def plan(f, depth="spot"):
    return common._drive(f, MANIFEST, check_mode=True, check_depth=depth, busid=BUSID)


def test_a_full_read_records_and_a_spot_plan_then_does_not_ask_the_board(host):
    f = Fake(MANIFEST["hash"])
    r = plan(f, "full")
    assert r["identity_checked"] == {"depth": "full", "reason": "asked"} and f.asked == 1
    rec = json.loads((host / "last-read" / "b1.json").read_text())
    assert rec["busid"] == BUSID and rec["serial"] == "S1" and rec["flasher"] == "Fake"
    r = plan(f)
    assert f.asked == 1 and r["changed"] is False
    assert r["identity_checked"]["depth"] == "spot"
    assert "no USB event on its port" in r["identity_checked"]["reason"]
    assert r["before"]["hash"] == MANIFEST["hash"]


def test_an_apply_always_asks_the_board(host):
    f = Fake(MANIFEST["hash"])
    plan(f, "full")
    r = common._drive(f, MANIFEST, check_mode=False, check_depth="spot", busid=BUSID)
    assert f.asked == 2 and r["identity_checked"]["depth"] == "full"
    assert "an apply asks the board" in r["identity_checked"]["reason"]


@pytest.mark.parametrize("change,why", [
    (lambda h: event(h, "remove"), "saw the board's port change"),
    (lambda h: event(h, "add", busid="9-9", serial="S1"), "saw the board's port change"),
    (lambda h: (h / "usb-present.json").write_text(json.dumps(
        {"pid": 1, "watching_since": stamp(time.time() + 5)})), "restarted"),
    (lambda h: (h / "usb-present.json").write_text(json.dumps({"devices": []})), "keeps no watch record"),
    (lambda h: (h / "plug-events.jsonl").write_text(
        json.dumps({"t": stamp(time.time() + 5), "action": "add", "busid": "7-7"}) + "\n"),
     "no longer reaches back"),
    (lambda h: (h / "last-read" / "b1.json").unlink(), "no last full read"),
])
def test_anything_that_could_hide_a_change_asks_the_board(host, change, why):
    f = Fake(MANIFEST["hash"])
    plan(f, "full")
    change(host)
    r = plan(f)
    assert f.asked == 2 and r["identity_checked"]["depth"] == "full", r
    assert why in r["identity_checked"]["reason"], r["identity_checked"]


def test_another_port_another_flasher_or_a_dead_watcher_asks_the_board(host, monkeypatch):
    f = Fake(MANIFEST["hash"])
    plan(f, "full")
    r = common._drive(f, MANIFEST, check_mode=True, check_depth="spot", busid="2-2")
    assert "on USB port 2-2" in r["identity_checked"]["reason"]
    rec, why = lastread.stand_in("b1", "Other", "bare", BUSID)
    assert rec is None and "through Fake as bare" in why
    monkeypatch.setattr(lastread, "_alive", lambda pid: False)
    rec, why = lastread.stand_in("b1", "Fake", "bare", BUSID)
    assert rec is None and "not running" in why


def test_a_record_of_another_image_is_never_used(host):
    f = Fake("a" * 64)
    plan(f, "full")
    r = plan(f)
    assert f.asked == 2 and r["changed"] is True
    assert r["identity_checked"] == {"depth": "full", "reason":
                                     "the last full read shows another image than the declared one"}


def test_a_write_replaces_the_record_and_a_failed_read_removes_it(host):
    f = Fake("a" * 64)
    common._drive(f, MANIFEST, check_mode=False, busid=BUSID)
    rec = json.loads((host / "last-read" / "b1.json").read_text())
    assert rec["identity"]["hash"] == MANIFEST["hash"]
    f.runs = None
    with pytest.raises(common.FlashError):
        plan(f, "full")
    assert not (host / "last-read" / "b1.json").exists()


def test_a_write_that_fails_leaves_no_record(host):
    class Broken(Fake):
        def write(self):
            raise common.FlashError("cable pulled")
    f = Broken("a" * 64)
    plan(f, "full")
    assert (host / "last-read" / "b1.json").exists()
    with pytest.raises(common.FlashError):
        common._drive(f, MANIFEST, check_mode=False, busid=BUSID)
    assert not (host / "last-read" / "b1.json").exists()


def test_a_flasher_without_the_form_keeps_no_record_and_always_asks(host):
    class Plain(Fake):
        spot_reads = ()
    f = Plain(MANIFEST["hash"])
    plan(f, "full")
    r = plan(f)
    assert f.asked == 2 and not (host / "last-read").exists()
    assert r["identity_checked"] == {"depth": "full", "reason": "identity has no spot form"}


def test_no_bench_host_no_record(tmp_path, monkeypatch):
    monkeypatch.setenv("RXRBENCH_HOST_STATE", str(tmp_path / "absent"))
    assert lastread.record("b1", "Fake", "bare", {"hash": "x"}, BUSID) is None
    assert lastread.record("b1", "Fake", "bare", {"hash": "x"}, None) is None
