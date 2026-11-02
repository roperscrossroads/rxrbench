"""The sim chip route's flow (flash/simchip.py): a simulated board's flash file backed up and
written back, against a stand-in process (the board) and a fake broker. The sim suite
(tests/sim) runs it on real native_sim boards; this pins its behaviour in seconds, so moving it
onto the one chip flow (docs/ARCHITECTURE.md#chip-and-region-flows) keeps it: a route supplies only its session, and
the flow is the same for every route."""
import gzip
import json
import os
import subprocess

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, simchip


class Exit(Exception):
    pass


class Module:
    def __init__(self, check_mode=False):
        self.check_mode, self.result = check_mode, None

    def exit_json(self, **kw):
        self.result = kw
        raise Exit()


def call(fn, *a):
    with pytest.raises(Exit):
        fn(*a)
    return a[0].result


class Broker:
    def __init__(self, rig):
        self.rig = rig

    def __enter__(self):
        self.rig.opened += 1
        return self

    def __exit__(self, *exc):
        return False

    def release(self, ttl=None):
        self.rig.released += 1
        return {"ok": True}

    def reacquire(self):
        self.rig.reacquired += 1
        return {"ok": True}


class Rig:
    """A sim board: a process whose pid is in `<run>/<board>.pid`, its flash file beside it."""

    def __init__(self, run):
        self.run, self.opened, self.released, self.reacquired = run, 0, 0, 0
        self.flash = run / "s1.flash"
        self.flash.write_bytes(os.urandom(8192))
        self.start()

    def start(self):
        self.proc = subprocess.Popen(["sleep", "60"])
        (self.run / "s1.pid").write_text(str(self.proc.pid))

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    r = Rig(run)
    monkeypatch.setattr(simchip.broker_client, "Board", lambda *a, **k: Broker(r))
    yield r
    r.stop()


def params(tmp_path, rig, **kw):
    return {"board": "s1", "family": "zapp", "dest": str(tmp_path / "backups"), "socket": None,
            "settle": 2.0, "snapshot": None, "run_dir": str(rig.run), "flasher": "esptool",
            "apply_allowed": True, **kw}


def test_a_backup_is_the_flash_file_with_a_record_shaped_as_a_real_chips(rig, tmp_path):
    got = call(simchip.backup, Module(), params(tmp_path, rig, snapshot="before"))
    meta = json.load(open(os.path.join(got["path"], "meta.json")))
    assert meta["chip"] == {"chip": "native-sim", "mac": "sim:s1", "flash_size": "8192B"}
    assert meta["kind"] == "chip" and meta["size"] == 8192 and meta["board"] == "s1"
    assert "route" not in meta and meta["family"] == "zapp" and meta["snapshot"] == "before"
    gz = os.path.join(got["path"], "flash.bin.gz")
    with gzip.open(gz) as g:
        assert g.read() == rig.flash.read_bytes()
    assert oct(os.stat(gz).st_mode & 0o777) == "0o600"
    assert os.path.realpath(os.path.join(tmp_path, "backups", "s1", "snapshot", "before")) == \
        os.path.realpath(got["path"])
    assert got["chip"] == meta["chip"] and got["sha256"] == meta["sha256"] and got["changed"]
    assert rig.proc.poll() is None, "the board goes on as if nothing happened"


def test_a_plan_of_a_backup_reads_nothing(rig, tmp_path):
    got = call(simchip.backup, Module(check_mode=True), params(tmp_path, rig))
    assert got["changed"] is False and "flash file" in got["msg"]
    assert not os.path.exists(os.path.join(tmp_path, "backups"))


def test_a_board_with_no_flash_file_or_no_process_is_not_backed_up(rig, tmp_path):
    rig.flash.unlink()
    with pytest.raises(common.FlashError, match="no flash file"):
        simchip.backup(Module(), params(tmp_path, rig))
    rig.stop()
    with pytest.raises(common.Unanswered, match="no running sim board"):
        simchip.backup(Module(), params(tmp_path, rig, dest=str(tmp_path / "other")))


def test_a_restore_writes_the_file_back_and_restarts_the_board(rig, tmp_path):
    path = call(simchip.backup, Module(), params(tmp_path, rig))["path"]
    before = rig.flash.read_bytes()
    rig.flash.write_bytes(b"\0" * 8192)
    plan = call(simchip.restore, Module(check_mode=True), params(tmp_path, rig), path)
    assert plan["changed"] is True and rig.flash.read_bytes() == b"\0" * 8192
    assert plan["path"] == path and rig.proc.poll() is None
    got = call(simchip.restore, Module(), params(tmp_path, rig), path)
    assert got["changed"] is True and rig.flash.read_bytes() == before
    assert rig.proc.wait(timeout=5) is not None, "stopped, for its supervisor to start again"
    assert rig.reacquired >= 1, "the console taken back"
    assert not os.path.exists(os.path.join(path, "flash.restore.bin"))


def test_a_restore_is_refused_for_another_board_another_chip_or_a_damaged_file(rig, tmp_path):
    path = call(simchip.backup, Module(), params(tmp_path, rig))["path"]
    with pytest.raises(common.FlashError, match="not 's2'"):
        simchip.restore(Module(), params(tmp_path, rig, board="s2"), path)
    meta_path = os.path.join(path, "meta.json")
    meta = json.load(open(meta_path))
    json.dump({**meta, "chip": {**meta["chip"], "chip": "esp32s3"}}, open(meta_path, "w"))
    with pytest.raises(common.FlashError, match="esp32s3 chip"):
        simchip.restore(Module(), params(tmp_path, rig), path)
    json.dump(meta, open(meta_path, "w"))
    with gzip.open(os.path.join(path, "flash.bin.gz"), "wb") as g:
        g.write(b"\1" * 8192)
    with pytest.raises(common.FlashError, match="SHA-256|does not hash"):
        simchip.restore(Module(), params(tmp_path, rig), path)
    assert rig.proc.poll() is None, "refused before the board is touched"


def test_a_plan_of_a_board_that_holds_the_backup_changes_nothing(rig, tmp_path):
    path = call(simchip.backup, Module(), params(tmp_path, rig))["path"]
    plan = call(simchip.restore, Module(check_mode=True), params(tmp_path, rig), path)
    assert plan["changed"] is False and "holds the backup already" in plan["msg"]


def test_a_failed_backup_leaves_no_directory(rig, tmp_path):
    rig.flash.unlink()
    with pytest.raises(common.FlashError, match="no flash file"):
        simchip.backup(Module(), params(tmp_path, rig))
    chips = os.path.join(tmp_path, "backups", "s1", "chip")
    assert not os.path.isdir(chips) or os.listdir(chips) == []


def test_a_whole_chip_erase_of_a_sim_board_needs_its_backup_then_removes_its_flash_file(rig, tmp_path):
    before = rig.flash.read_bytes()
    with pytest.raises(common.FlashError, match="no backup of this chip"):
        simchip.erase(Module(), params(tmp_path, rig))
    assert rig.flash.read_bytes() == before and rig.proc.poll() is None
    path = call(simchip.backup, Module(), params(tmp_path, rig))["path"]
    plan = call(simchip.erase, Module(check_mode=True), params(tmp_path, rig))
    assert plan["backups"] == [os.path.basename(path)] and plan["size"] == 8192
    assert rig.flash.read_bytes() == before, "a plan erases nothing"
    got = call(simchip.erase, Module(), params(tmp_path, rig))
    assert got["changed"] and "starts on a blank one" in got["msg"] and not rig.flash.exists()
    assert rig.proc.wait(timeout=5) is not None, "stopped, for its supervisor to start again"


def test_a_sim_board_that_is_not_running_is_not_erased(rig, tmp_path):
    rig.stop()
    with pytest.raises(common.Unanswered, match="no running sim board"):
        simchip.erase(Module(), params(tmp_path, rig, without_backup=True))
    assert rig.flash.exists()
