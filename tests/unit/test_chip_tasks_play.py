"""A chip a plugin's own flasher writes (core-portability P6): `just backup`, `just
restore-chip` and `just erase-chip` include the task files its family names (Family.chip_tasks), past
core's go-ahead, where core has no module that knows the flasher (fixture.widgets' probechip)."""
import os
import subprocess

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOKS = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "playbooks")
FIXTURES = os.path.join(REPO, "tests", "fixtures", "collections")
TASKS = {"backup": "chip_backup_probe", "restore": "chip_restore_probe", "erase": "erase_probe"}


def run(tmp_path, playbook, *args, allowed=True, repo=None):
    """`repo`: bench_repo, where the tool's own scripts are (a restore forgets a bootloader read
    with tools/facts_note.py); a temp directory by default, so a backup lands there."""
    image = tmp_path / "store" / "probe-img"
    image.mkdir(parents=True, exist_ok=True)
    chip = tmp_path / "chip.bin"
    if not chip.exists():
        chip.write_bytes(b"\xff" * (1 << 20))
    board = {"bench_board": "p1", "bench_via": None, "bench_family": "probechip",
             "bench_plugin": "fixture.widgets", "bench_flasher": "probeflash", "bench_can": ["chip"],
             "bench_chip_tasks": TASKS, "bench_families": {}, "bench_image": str(image),
             "bench_apply_allowed": allowed, "probechip_flash": str(chip)}
    inv = {"boards": {"hosts": {"p1": board},
                      "vars": {"ansible_connection": "local", "bench_layout": "t",
                               "ansible_python_interpreter": os.path.join(REPO, "venv", "bin", "python"),
                               "bench_repo": repo or str(tmp_path), "bench_runs_dir": str(tmp_path / "runs"),
                               "board_host_store": str(tmp_path / "host-store"),
                               "bench_facts_dir": str(tmp_path / "facts")}}}
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump(inv))
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=os.pathsep.join([os.path.join(REPO, "collections"), FIXTURES]),
               ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0")
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-playbook"), "-i", str(tmp_path / "hosts.yml"),
                        os.path.join(PLAYBOOKS, playbook), *args], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=180)
    return r.returncode, " ".join((r.stdout + r.stderr).split())


def test_a_plugins_chip_is_backed_up_by_its_familys_task_file(tmp_path):
    rc, out = run(tmp_path, "backup.yml")
    assert rc == 0, out
    assert (tmp_path / "backups" / "p1" / "chip" / "p1" / "flash.bin").read_bytes() == (tmp_path / "chip.bin").read_bytes(), out
    assert "p1: chip p1 (1MB)" in out, out


def test_a_plugins_chip_is_erased_and_written_back_past_the_go_ahead(tmp_path):
    run(tmp_path, "backup.yml", allowed=False)      # creates the stand-in chip
    chip = tmp_path / "chip.bin"
    held = b"firmware" + chip.read_bytes()[8:]
    chip.write_bytes(held)
    rc, out = run(tmp_path, "backup.yml")
    assert rc == 0, out
    rc, out = run(tmp_path, "erase-chip.yml")
    assert rc == 0 and "erased through the probe" in out, out
    assert chip.read_bytes() == b"\xff" * (1 << 20)
    rc, out = run(tmp_path, "restore-chip.yml", repo=REPO)
    assert rc == 0 and "written back through the probe" in out, out
    assert chip.read_bytes() == held


def test_without_the_go_ahead_the_plugins_task_file_is_never_entered(tmp_path):
    for play in ("restore-chip.yml", "erase-chip.yml"):
        rc, out = run(tmp_path, play, allowed=False)
        assert rc != 0 and "does not set bench_apply_allowed" in out, out
        assert "through the probe" not in out, out


def test_a_plugins_chip_with_no_backup_is_erased_only_when_said_so(tmp_path):
    chip = tmp_path / "chip.bin"
    held = b"firmware" + b"\xff" * ((1 << 20) - 8)
    chip.write_bytes(held)                          # no backup of it anywhere
    rc, out = run(tmp_path, "erase-chip.yml")
    assert rc != 0 and "no chip backup in backups/p1/chip" in out and "just backup t p1" in out, out
    assert chip.read_bytes() == held, "refused: nothing erased"
    rc, out = run(tmp_path, "erase-chip.yml", "-e", "erase_without_backup=true")
    assert rc == 0 and "erased through the probe" in out, out
    assert chip.read_bytes() == b"\xff" * (1 << 20)


def test_a_region_erase_never_erases_the_whole_chip(tmp_path):
    """A plugin chip with no region level: `just erase` refuses and names erase-chip."""
    chip = tmp_path / "chip.bin"
    held = b"firmware" + b"\xff" * ((1 << 20) - 8)
    chip.write_bytes(held)
    rc, out = run(tmp_path, "erase.yml")
    assert rc != 0 and "no config regions to erase" in out and "just erase-chip t p1" in out, out
    assert chip.read_bytes() == held
