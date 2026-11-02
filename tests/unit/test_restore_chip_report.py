"""restore-chip's report, rendered as ansible renders it: a plan, or a backup that recorded no
identity, prints no empty fields or "came back as (the backup recorded no identity)", and a
snapshot is said to be one."""
import os

import pytest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core",
                        "playbooks", "templates", "restore_chip_report.j2")
ZT = {"family": "zephyrtastic", "version": "0.4.47+45", "build": "1117570e", "hash": None}


def report(restore, check=False):
    text = open(TEMPLATE).read()
    return Templar(loader=DataLoader(), variables={
        "bench_board": "b1", "restore": restore, "ansible_check_mode": check}).template(
        trust_as_template(text)).strip()


def test_a_plan_says_what_would_be_written_and_no_empty_fields():
    out = report({"path": "/b/chip/1", "taken": "1", "recorded": ZT, "snapshot": None,
                  "msg": "would be written to aa:bb (16MB)"}, check=True)
    assert out == ("b1: backup /b/chip/1, taken 1, running zephyrtastic 0.4.47+45; "
                   "would be written to aa:bb (16MB)")


def test_a_written_snapshot_says_it_is_one_and_how_the_board_came_back():
    out = report({"path": "/b/chip/2", "taken": "2", "recorded": ZT, "snapshot": "zt",
                  "written_blocks": 9, "blocks": 256, "came_back_as": ZT})
    assert out == ("b1: snapshot zt /b/chip/2, taken 2, running zephyrtastic 0.4.47+45; "
                   "9 of 256 blocks written; came back as zephyrtastic 0.4.47+45")


@pytest.mark.parametrize("check, tail", [(True, ""), (False, "; not read back: the backup recorded no identity")])
def test_a_backup_with_no_identity_says_so_once(check, tail):
    out = report({"path": "/b/chip/3", "taken": "3", "recorded": None, "snapshot": None,
                  **({} if check else {"written_blocks": 2, "blocks": 64})}, check=check)
    assert out == "b1: backup /b/chip/3, taken 3" + ("" if check else "; 2 of 64 blocks written") + tail


def test_a_picoboot_restore_reports_its_own_message():
    out = report({"path": "/b/chip/4", "taken": "4", "snapshot": None,
                  "msg": "18 sectors written; the chip hashes to the backup"})
    assert out.endswith("; 18 sectors written; the chip hashes to the backup")


def test_a_plan_says_how_many_blocks_it_would_write():
    out = report({"path": "/b/chip/5", "taken": "5", "recorded": ZT, "snapshot": "zc",
                  "differing_blocks": 9, "blocks": 256, "msg": "would be written to aa:bb (16MB)"},
                 check=True)
    assert out.endswith("; would be written to aa:bb (16MB); 9 of 256 blocks to write"), out
