"""Whole-chip backups: what the helper says, and when a backup may be written back."""
import json
import os
import sys

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import chip, common
from ansible_collections.rxrbench.core.plugins.modules import chip_restore

CHIP = {"mac": "aa:bb:cc:00:11:22", "flash_size": "16MB"}
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _helper():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "chip_blocks", os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "bin", "chip_blocks.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)            # esptool is imported only to talk to a chip
    return mod


def test_blocks_to_read_or_write_go_in_contiguous_runs():
    assert _helper().runs([0, 1, 2, 5, 7, 8]) == [[0, 3], [5, 1], [7, 2]]
    assert _helper().runs([]) == []


def _fake(tmp_path, out, rc=0):
    script = tmp_path / "fake_blocks.py"
    script.write_text("import sys\nprint('esptool chatter')\nprint(%r)\nsys.exit(%d)\n" % (out, rc))
    return {"python": sys.executable, "chip_blocks": str(script), "esptool": "/bin/true"}


def test_the_helper_s_last_line_is_its_answer(tmp_path):
    out = chip.blocks(_fake(tmp_path, json.dumps({"ok": True, "mac": "aa:bb:cc:00:11:22",
                                                  "size": 16 << 20, "read_blocks": 3})),
                      "/dev/null", "info")
    assert out["mac"] == "aa:bb:cc:00:11:22" and out["flash_size"] == "16MB"


def test_a_helper_that_fails_is_a_flash_error(tmp_path):
    with pytest.raises(common.FlashError, match="block 4 read back wrong"):
        chip.blocks(_fake(tmp_path, json.dumps({"ok": False, "error": "block 4 read back wrong"}),
                          rc=1), "/dev/null", "read", "x")


OLD_USAGE = """A whole ESP32 flash chip, block by block, in one ROM-loader session.

    chip_blocks.py <port> info              {mac, flash_size, size}
    chip_blocks.py <port> match <in.bin> <lo:hi>... [--mac M]
Runs with the bench host's esptool venv (~/.venv-esp/bin/python)."""


def _usage(tmp_path, text):
    script = tmp_path / "old_blocks.py"
    script.write_text("import sys\nsys.exit(%r)\n" % text)
    return {"python": sys.executable, "chip_blocks": str(script), "esptool": "/bin/true"}


def test_a_bench_host_copy_older_than_the_command_says_run_hosts_setup(tmp_path):
    """A copy from before `layout` prints its usage; the caller says the copy is out of date,
    names it and the way out, and that nothing was written."""
    tools = _usage(tmp_path, OLD_USAGE)
    with pytest.raises(common.FlashError) as e:
        chip.blocks(tools, "/dev/null", "layout")
    assert str(e.value) == (f"the bench host's chip_blocks.py ({tools['chip_blocks']}) has no "
                            "`layout` command: it is older than this checkout. Bring the host's "
                            "tools up to date with `just hosts-setup <layout>`; nothing was written")
    # a copy that says so in JSON, as this one does
    tools = _fake(tmp_path, json.dumps({"ok": False, "unknown_command": "layout"}), rc=1)
    with pytest.raises(common.FlashError, match="has no `layout` command: it is older"):
        chip.blocks(tools, "/dev/null", "layout")


def test_a_command_the_copy_has_called_wrongly_is_said_as_that(tmp_path):
    with pytest.raises(common.FlashError, match=r"called with arguments `match` does not take \(match x\)"):
        chip.blocks(_usage(tmp_path, OLD_USAGE), "/dev/null", "match", "x")


def test_the_helper_names_a_command_it_does_not_have_as_json():
    with pytest.raises(SystemExit) as e:
        _helper().main(["/dev/null", "frobnicate"])
    answer = json.loads(str(e.value.code))
    assert answer["unknown_command"] == "frobnicate" and answer["ok"] is False
    assert "layout" in answer["commands"]
    with pytest.raises(SystemExit) as e:           # a known command called wrongly: its usage
        _helper().main(["/dev/null", "layout", "not-a-range"])
    assert "chip_blocks.py <port> layout" in str(e.value.code)


def meta(**over):
    m = {"board": "board-a", "chip": dict(CHIP), "sha256": "ab" * 32}
    m.update(over)
    return m


def test_a_backup_goes_back_to_its_own_chip_only():
    assert chip.check_backup(meta(), CHIP, "board-a", "ab" * 32) == []
    other = {**CHIP, "mac": "aa:bb:cc:00:11:23"}
    why = chip.check_backup(meta(), other, "board-a", "ab" * 32)
    assert len(why) == 1 and "two boards one node" in why[0]
    assert "not 'board-b'" in chip.check_backup(meta(), CHIP, "board-b", "ab" * 32)[0]
    assert "16MB" in chip.check_backup(meta(), {**CHIP, "flash_size": "8MB"}, "board-a",
                                       "ab" * 32)[0]
    assert "SHA-256" in chip.check_backup(meta(), CHIP, "board-a", "cd" * 32)[0]


def test_an_image_survives_compression(tmp_path):
    raw = tmp_path / "flash.bin"
    raw.write_bytes(os.urandom(4096) + b"\xff" * 65536)
    chip.compress(str(raw), str(raw) + ".gz")
    chip.expand(str(raw) + ".gz", str(tmp_path / "back.bin"))
    assert chip.sha256_file(str(tmp_path / "back.bin")) == chip.sha256_file(str(raw))


def test_chip_backups_are_named_like_settings_backups(tmp_path):
    for stamp in ("20261004T150000Z", "20261004T160000Z", "20261004T170000Z"):
        d = tmp_path / "board-a" / "chip" / stamp
        d.mkdir(parents=True)
        if stamp != "20261004T170000Z":            # unfinished: no meta.json
            (d / "meta.json").write_text(json.dumps(meta()))
    newest = str(tmp_path / "board-a" / "chip" / "20261004T160000Z")
    assert chip_restore.backup_path(str(tmp_path), "board-a", "latest") == newest
    assert chip_restore.backup_path(str(tmp_path), "board-a", "20261004T150000Z") == \
        str(tmp_path / "board-a" / "chip" / "20261004T150000Z")


def test_a_snapshot_is_found_by_its_name(tmp_path):
    d = tmp_path / "board-a" / "chip" / "20261004T150000Z"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta()))
    (tmp_path / "board-a" / "snapshot").mkdir()
    os.symlink("../chip/20261004T150000Z", tmp_path / "board-a" / "snapshot" / "topo-2")
    assert chip_restore.backup_path(str(tmp_path), "board-a", "topo-2") == str(d)
    assert chip_restore.backup_path(str(tmp_path), "board-a", "20261004T150000Z") == str(d)


def test_latest_is_the_newest_backup_not_a_snapshot(tmp_path):
    """`restore-chip` (latest) after a snapshot puts back the last backup, not the snapshot: a
    snapshot is put back by its name."""
    root = tmp_path / "board-a" / "chip"
    for stamp, snap in (("20261103T204952Z", None), ("20261103T205448Z", "zt"),
                        ("20261103T210209Z", "zc")):
        (root / stamp).mkdir(parents=True)
        (root / stamp / "meta.json").write_text(json.dumps({**meta(), **({"snapshot": snap} if snap else {})}))
    assert chip_restore.backup_path(str(tmp_path), "board-a", "latest") == \
        str(root / "20261103T204952Z")
    assert chip_restore.backup_path(str(tmp_path), "board-a", "20261103T210209Z") == \
        str(root / "20261103T210209Z"), "a snapshot's stamp, given, is still that backup"


def test_latest_with_only_snapshots_names_them(tmp_path):
    root = tmp_path / "board-a" / "chip" / "20261103T210209Z"
    root.mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({**meta(), "snapshot": "zc"}))
    with pytest.raises(common.FlashError, match=r"only snapshots \(zc\): name one"):
        chip_restore.backup_path(str(tmp_path), "board-a", "latest")


# ---- the helper's whole-chip erase ----------------------------------------------------------

class FakeEsp:
    """A 256 KB chip as the loader's stub sees it: its MAC, its size, an erase, block hashes."""

    def __init__(self, mac=(0xAA, 0xBB, 0xCC, 0, 0x11, 0x22), sticky=False):
        self.mac, self.sticky = mac, sticky
        self.flash = bytearray(os.urandom(0x40000))
        self.erased = 0

    def read_mac(self):
        return self.mac

    def flash_id(self):
        return 18 << 16                      # 1 << 18 bytes

    def get_chip_description(self):
        return "ESP32-S3"

    def erase_flash(self):
        self.erased += 1
        if not self.sticky:
            self.flash[:] = b"\xff" * len(self.flash)

    def flash_md5sum(self, off, n):
        import hashlib
        return hashlib.md5(bytes(self.flash[off:off + n])).hexdigest()


def _erase(monkeypatch, capsys, esp, *args):
    mod = _helper()
    monkeypatch.setattr(mod, "connect", lambda port: esp)
    try:
        mod.main(["/dev/null", "erase", *args])
        code = 0
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1] if out else str(code))


def test_the_helpers_whole_chip_erase_needs_the_mac_and_proves_every_block(monkeypatch, capsys):
    esp = FakeEsp()
    _code, out = _erase(monkeypatch, capsys, esp)
    assert out["ok"] is False and "needs --mac" in out["error"] and esp.erased == 0
    _code, out = _erase(monkeypatch, capsys, esp, "--mac", "aa:bb:cc:00:11:23")
    assert out["ok"] is False and "not this board" in out["error"] and esp.erased == 0
    code, out = _erase(monkeypatch, capsys, esp, "--mac", "AA:BB:CC:00:11:22")
    assert code == 0 and out["erased"] is True and out["blocks"] == 4 and esp.erased == 1
    assert esp.flash == b"\xff" * 0x40000


def test_a_chip_that_does_not_read_erased_after_the_erase_is_a_failure(monkeypatch, capsys):
    _code, out = _erase(monkeypatch, capsys, FakeEsp(sticky=True), "--mac", "aa:bb:cc:00:11:22")
    assert out["ok"] is False and "4 of 4 blocks do not read erased" in out["error"]
