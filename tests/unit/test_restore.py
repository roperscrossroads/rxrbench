import hashlib
import json
import os

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, region_flow

MAP = [("storage", 0x420000, 0x8000), ("bulk", 0x430000, 0x70000)]


def backup(tmp_path, board="board-a", regions=MAP, stamp="20261002T184400Z-3f3de48a",
           data=None):
    d = tmp_path / board / stamp
    d.mkdir(parents=True)
    recs = []
    for name, off, size in regions:
        blob = (data or {}).get(name, bytes([len(name)]) * size)
        (d / f"{name}.bin").write_bytes(blob)
        recs.append({"name": name, "offset": f"{off:#x}", "size": size, "file": f"{name}.bin",
                     "sha256": hashlib.sha256(blob).hexdigest()})
    (d / "meta.json").write_text(json.dumps({"board": board, "regions": recs,
                                             "device": {"build": "3f3de48a"}}))
    return str(d)


def test_a_good_backup_of_this_board_passes(tmp_path):
    meta, refusals = region_flow.check_backup(backup(tmp_path), "board-a", MAP)
    assert refusals == []
    assert [r["name"] for r in meta["regions"]] == ["storage", "bulk"]


def test_another_board_s_backup_is_refused_it_would_clone_the_identity(tmp_path):
    _meta, refusals = region_flow.check_backup(backup(tmp_path, board="board-b"), "board-a", MAP)
    assert any("two nodes one id" in r for r in refusals)


def test_a_changed_file_is_refused(tmp_path):
    d = backup(tmp_path)
    with open(os.path.join(d, "bulk.bin"), "r+b") as f:
        f.write(b"\xff")
    _meta, refusals = region_flow.check_backup(d, "board-a", MAP)
    assert any("SHA-256" in r for r in refusals)


def test_a_truncated_file_is_refused(tmp_path):
    d = backup(tmp_path)
    with open(os.path.join(d, "storage.bin"), "r+b") as f:
        f.truncate(100)
    _meta, refusals = region_flow.check_backup(d, "board-a", MAP)
    assert any("100 B" in r for r in refusals)


def test_a_map_that_moved_the_settings_is_refused(tmp_path):
    moved = [("storage", 0x410000, 0x8000), ("bulk", 0x430000, 0x70000)]
    _meta, refusals = region_flow.check_backup(backup(tmp_path), "board-a", moved)
    assert any("storage: read at 0x420000" in r for r in refusals)


def test_a_region_the_image_does_not_have_is_refused(tmp_path):
    _meta, refusals = region_flow.check_backup(backup(tmp_path), "board-a", MAP[:1])
    assert any("bulk: the board's image has no such region" in r for r in refusals)


def test_a_region_the_backup_lacks_is_left_alone_and_said(tmp_path):
    meta, refusals = region_flow.check_backup(backup(tmp_path, regions=MAP[:1]), "board-a", MAP)
    assert refusals == []
    assert meta["left_alone"] == ["bulk"]


def test_a_backup_without_storage_is_refused(tmp_path):
    _meta, refusals = region_flow.check_backup(backup(tmp_path, regions=MAP[1:]), "board-a", MAP)
    assert any("no storage" in r for r in refusals)


def test_no_meta_is_refused(tmp_path):
    _meta, refusals = region_flow.check_backup(str(tmp_path), "board-a", MAP)
    assert refusals and "meta.json" in refusals[0]


def test_latest_is_the_newest_complete_backup(tmp_path):
    backup(tmp_path, stamp="20261001T000000Z-aaaaaaaa")
    newest = backup(tmp_path, stamp="20261002T000000Z-bbbbbbbb")
    (tmp_path / "board-a" / "20261003T000000Z-unfinished").mkdir()   # no meta.json
    assert region_flow.latest(str(tmp_path), "board-a") == newest


def test_a_backup_is_named_latest_by_its_name_or_by_a_path(tmp_path):
    older = backup(tmp_path, stamp="20261001T000000Z-aaaaaaaa")
    newest = backup(tmp_path, stamp="20261002T000000Z-bbbbbbbb")
    assert region_flow.backup_path(str(tmp_path), "board-a", "latest") == newest
    assert region_flow.backup_path(str(tmp_path), "board-a", "20261001T000000Z-aaaaaaaa") == older
    assert region_flow.backup_path(str(tmp_path), "board-a", older) == older


def test_no_backup_at_all_is_an_error(tmp_path):
    with pytest.raises(common.FlashError, match="no backup of board-a"):
        region_flow.latest(str(tmp_path), "board-a")
