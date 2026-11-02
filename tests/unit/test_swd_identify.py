"""What diag asks a chip behind a debug probe (modules/swd_identify.py): its identity, read
through its own probe with the unique id as the guard and without halting it; a chip that is
reached only under reset is not read, and the record says so."""
import json

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, swd
from ansible_collections.rxrbench.core.plugins.modules import swd_identify as mod


class Mod:
    check_mode = True

    def __init__(self, **params):
        self.params = {"board": "nucleof7", "probe": "stlink-nucleof7", "chip": "STM32F767ZI",
                       "uid": "003a001a3034511930363235", "nrst": False, "tools": {}, **params}
        self.result = None

    def exit_json(self, **kw):
        self.result = kw
        raise SystemExit(0)


def run(monkeypatch, call=None, **params):
    m, seen = Mod(**params), []

    def fake_call(self, *args, **_kw):
        seen.append((self.probe, self.uid, args))
        return call(*args)
    monkeypatch.setattr(mod, "AnsibleModule", lambda **_kw: m)
    monkeypatch.setattr(swd.Probe, "call", fake_call)
    with pytest.raises(SystemExit):
        mod.main()
    return m.result, seen


def test_the_chip_is_read_by_look_through_its_probe_with_its_uid(monkeypatch):
    out, seen = run(monkeypatch, lambda *a: {"uid": "003a001a3034511930363235", "flash_size": 2097152})
    assert seen == [("stlink-nucleof7", "003a001a3034511930363235", ("id", "--look"))]
    r = out["results"][0]
    assert out["changed"] is False and r["ok"] and r["name"] == "swd-id"
    assert json.loads(r["output"])["flash_size"] == 2097152


def test_a_chip_that_is_not_there_is_recorded_not_failed(monkeypatch):
    def gone(*_a):
        raise common.FlashError("nucleof7: the chip behind the probe has unique id ffff")
    out, _seen = run(monkeypatch, gone)
    assert out["results"][0]["ok"] is False and "unique id ffff" in out["results"][0]["output"]


def test_a_chip_reached_only_under_reset_is_not_read(monkeypatch):
    out, seen = run(monkeypatch, lambda *a: {}, nrst=True)
    assert seen == [] and "under reset" in out["results"][0]["output"]
