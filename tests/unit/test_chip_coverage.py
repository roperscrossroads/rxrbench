"""Backup, snapshot and switch refuse a layout with boards they cannot cover, and name them,
unless -e partial=true leaves those boards out (playbooks/tasks/coverage.yml)."""
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYS = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "playbooks")


def play(tmp_path, playbook, inventory, *args):
    inv = tmp_path / "hosts.yml"
    inv.write_text(inventory)
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=os.path.join(REPO, "collections"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0")
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-playbook"), "-i", str(inv),
                        os.path.join(PLAYS, playbook), "-e", "snapshot_name=topo-2", *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


# Two boards a whole-chip path cannot reach; the plays stop before any module runs.
NONE_COVERED = """\
boards:
  hosts:
    b1: {{bench_board: b1, bench_can: [regions], bench_flasher: smp}}
    b2: {{bench_board: b2, bench_can: [], bench_flasher: swd}}
  vars:
    ansible_connection: local
    ansible_python_interpreter: {python}
"""


@pytest.fixture
def inventory():
    return NONE_COVERED.format(python=os.path.join(REPO, "venv", "bin", "python"))


@pytest.mark.parametrize("playbook,args", [("snapshot.yml", ()), ("switch.yml", ("--check",))])
def test_boards_without_a_chip_path_are_named_and_the_play_refused(tmp_path, inventory, playbook, args):
    rc, out = play(tmp_path, playbook, inventory, *args)
    assert rc != 0, out
    assert "no whole-chip path for b1, b2 (smp, swd)" in out, out
    assert "partial=true" in out, out


@pytest.mark.parametrize("playbook,args", [("snapshot.yml", ()), ("switch.yml", ("--check",))])
def test_partial_leaves_them_out_and_says_so(tmp_path, inventory, playbook, args):
    rc, out = play(tmp_path, playbook, inventory, "-e", "partial=true", *args)
    assert rc == 0, out
    assert "b1: no whole-chip path for smp boards; left out" in out, out
    assert "b2: no whole-chip path for swd boards; left out" in out, out


def test_a_limit_to_covered_boards_needs_no_partial(tmp_path, inventory):
    """--limit narrows what has to be covered: the check is over the play's hosts."""
    covered = inventory.replace("bench_can: [],", "bench_can: [chip],")
    rc, out = play(tmp_path, "switch.yml", covered, "--check", "--limit", "b2",
                   "-e", "bench_apply_allowed=false")
    # b2 is covered: the coverage check passes and the play reaches the write (which fails here,
    # there being no board); what matters is that it was not refused for coverage.
    assert "no whole-chip path" not in out, out
    assert "Write the snapshot back" in out, out


def test_a_backup_names_boards_with_no_backup_path_and_says_what_does_cover_them(tmp_path, inventory):
    """A UF2 board has no region or chip level: a backup that skipped it quietly looked like a
    backup of the layout (seen on a fresh bench)."""
    rc, out = play(tmp_path, "backup.yml", inventory)
    assert rc != 0, out
    assert "no backup path for b2 (swd)" in out and "keys-export" in out, out
    assert "b1" not in out.split("no backup path for")[1].split("(")[0], "b1 has a region level"


def test_a_partial_backup_says_who_was_left_out(tmp_path, inventory):
    only_uncovered = inventory.replace("bench_can: [regions]", "bench_can: []")
    rc, out = play(tmp_path, "backup.yml", only_uncovered, "-e", "partial=true")
    assert rc == 0, out
    assert "b1: no backup path for smp boards; left out" in out, out
