"""A flash marks its board busy on its bench host while it runs, and only there."""
import json
import os
import time

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common


def test_a_mark_holds_while_the_flash_runs_and_a_short_while_after(tmp_path, monkeypatch):
    monkeypatch.setenv("RXRBENCH_HOST_STATE", str(tmp_path))
    t0 = time.time()
    with common.busy("pog1", keys=["1B2B3B4B5B6B7B8B", None]):
        mark = json.loads((tmp_path / "busy" / "pog1.json").read_text())
        assert mark["board"] == "pog1" and mark["keys"] == ["1B2B3B4B5B6B7B8B"]
        assert mark["until"] >= t0 + common.BUSY_TTL and mark["busids"] == []
    after = json.loads((tmp_path / "busy" / "pog1.json").read_text())
    assert t0 + common.BUSY_GRACE <= after["until"] < t0 + common.BUSY_GRACE + 30


def test_a_machine_with_no_host_state_gets_no_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("RXRBENCH_HOST_STATE", str(tmp_path / "absent"))
    with common.busy("pog1"):
        pass
    assert not os.path.exists(tmp_path / "absent")
