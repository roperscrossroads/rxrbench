"""Every play that reads, writes or erases a whole chip hands the module the board's flash route:
the swd flasher finds its probe, chip and uid there and nowhere else, so a play without it fails
on an SWD board with "the board's swd route names no probe" (seen on hardware: `snapshot`, while
`backup`, `restore-chip` and `erase-chip` passed the route and worked)."""
import glob
import os

import yaml

PLAYBOOKS = os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections", "rxrbench",
                         "core", "playbooks", "*.yml")
CHIP_MODULES = ("rxrbench.core.chip_backup", "rxrbench.core.chip_restore", "rxrbench.core.chip_erase")


def chip_tasks():
    for path in sorted(glob.glob(PLAYBOOKS)):
        with open(path) as fh:
            plays = yaml.safe_load(fh)
        for play in plays:
            for task in play.get("tasks", []):
                for mod in CHIP_MODULES:
                    if mod in task:
                        yield os.path.basename(path), task["name"], task[mod]


def test_every_whole_chip_play_hands_the_module_the_flash_route():
    found = list(chip_tasks())
    assert {f for f, _, _ in found} >= {"backup.yml", "restore-chip.yml", "erase-chip.yml", "snapshot.yml", "switch.yml"}
    without = [f"{f}: {name}" for f, name, args in found if "route" not in args]
    assert not without, without
    for _, _, args in found:
        assert "bench_routes" in args["route"] and ".flash" in args["route"]
