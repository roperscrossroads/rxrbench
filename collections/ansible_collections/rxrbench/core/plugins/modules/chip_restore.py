#!/usr/bin/python
"""Write a whole-chip backup back onto the ESP32 board it was read from."""

DOCUMENTATION = r"""
module: chip_restore
short_description: Restore a board's whole flash chip from a chip_backup
description:
  - "Which route writes the chip goes by the board's flasher, as for C(chip_backup). A chip
    behind a debug probe (flasher C(swd)) is written whole through the probe unless it holds
    the backup already, then read again, which must hash to it; the backup must have been read
    from this chip (its unique id)."
  - "Writes every byte of a C(chip_backup) image back over the board's flash: bootloader,
    partition layout, applications and config. The board comes back as the node it was
    when the backup was taken, whatever ran on it since (another firmware family, a stock
    firmware, an erased chip)."
  - "Refused before anything is written unless the backup is this board's, read from this
    chip (the factory MAC the ROM loader reports now must be the one recorded: the image
    holds the node's private key, and two boards with one key are one node twice), of the
    same flash size, and its image still hashes to what was read."
  - "Only the blocks that differ from the image are written (the chip hashes every 64 KB block
    first, and each written block again after), in one ROM-loader session; then the board is
    reset: a board whose config only changed is restored in well under a minute. If the backup recorded what the board ran, the board must come back
    as exactly that."
  - With check mode every check is made, the chip is identified (which resets the board), and
    nothing is written.
options:
  board:
    description: The board's name.
    type: str
    required: true
  backup:
    description: C(latest) for this board's newest chip backup under C(dest), a backup's name
      (its stamp), a snapshot's name (C(chip_backup) C(snapshot)), or a directory on this
      host.
    type: str
    default: latest
  dest:
    description: Where backups are kept on this host (as for C(chip_backup)).
    type: path
    default: ~/rxrbench/backups
  chip:
    description: The board's chip, as esptool names it (C(bench_mcu), from the board's type in catalog/boards.yml).
    type: str
  app_manager_overwrite:
    description: The layout's go-ahead (C(bench_app_manager_overwrite)) to write a backup that
      holds no app manager onto a chip whose partition table shows one with images installed
      beside it. Refused without it; a backup that holds the manager too goes back as it is.
    type: bool
    default: false
  tools:
    description: Where the bench host keeps its tools. Keys C(esptool), C(python) (with the
      esptool library), C(chip_blocks), C(devreg).
    type: dict
    default: {}
  flasher:
    description: How the board is flashed (C(bench_flasher)). A simulated board is told by I(run_dir), not by this.
    type: str
  route:
    description: The board's flash route as the registry gives it (C(bench_routes.flash)). A
      chip behind a debug probe (flasher C(swd)) is found by its C(probe), C(chip) (the part,
      for openocd) and C(uid) (the chip's unique id, checked in every session), with C(nrst)
      when the probe's reset line is wired.
    type: dict
  chip_id:
    description: A picoboot board's chip id (the registry's C(usb_serial)), which picotool finds and checks it by.
    type: str
  uf2:
    description: A picoboot board's type's C(uf2) block (its whole flash, C(app); the ways into its bootloader, C(enter)).
    type: dict
  enter:
    description: A picoboot board's ways into its bootloader, over the type's.
    type: list
    elements: str
  manual_wait:
    description: Seconds a picoboot board's C(manual) way waits for BOOTSEL.
    type: float
    default: 0
  enter_wait:
    description: Seconds to wait for a picoboot board's bootloader after each way in.
    type: float
  ask_timeout:
    description: Seconds a touch or shell command may take on a picoboot board.
    type: float
  run_dir:
    description: A simulated board's run directory (C(bench_sim_run)), where its flash file is. Given, the board's chip is that file, whatever flashes it.
    type: path
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  settle:
    description: Seconds the board has to come back after the write.
    type: float
    default: 90.0
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode) needs none.
    type: bool
    default: false
  families:
    description: The device families as data, the inventory's C(bench_families). It says how
      a board's identity is read and what an image of a family must match.
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Put the board's newest chip backup back
  rxrbench.core.chip_restore:
    board: "{{ bench_board }}"
"""

RETURN = r"""
path:
  description: The backup that was (or in check mode would be) written.
  returned: always
  type: str
came_back_as:
  description: What the board said it ran after the write.
  returned: when written and the backup recorded the board's identity
  type: dict
app_manager:
  description: What the chip's partition table and the backup's each hold (C(chip), C(backup)),
    in words.
  returned: when the chip was read
  type: dict
"""

import json
import os

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, stateline
from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils.flash import access, common
from ansible_collections.rxrbench.core.plugins.module_utils.flash.region_flow import latest


def latest_backup(root):
    """The newest chip backup under `root` (<dest>/<board>) that is not a snapshot's copy: a
    snapshot is a named configuration, put back by its name, and `latest` meaning the newest
    snapshot would take a board to a setup made for a test instead of its last backup."""
    chips = os.path.join(root, "chip")
    found, snaps = [], []
    for d in sorted(os.listdir(chips)) if os.path.isdir(chips) else []:
        try:
            with open(os.path.join(chips, d, "meta.json")) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        (snaps if meta.get("snapshot") else found).append((d, meta.get("snapshot")))
    if found:
        return os.path.join(chips, found[-1][0])
    if snaps:
        raise common.FlashError(f"no chip backup under {chips}, only snapshots ("
                                f"{', '.join(sorted({s for _d, s in snaps}))}): name one")
    return latest(root, "chip")    # says there is none


def backup_path(dest, board, backup):
    """`latest` (the newest chip backup, not a snapshot), a backup's stamp, a snapshot's name
    (chip_backup `snapshot`), or a path."""
    root = os.path.join(os.path.expanduser(dest), board)
    if backup == "latest":
        return latest_backup(root)
    if os.sep not in backup:
        stamp = os.path.join(root, "chip", backup)
        snap = os.path.join(root, "snapshot", backup)
        if not os.path.isdir(stamp) and os.path.isdir(snap):
            return os.path.realpath(snap)
        return stamp
    return os.path.expanduser(backup)


def main():
    module = AnsibleModule(
        argument_spec={
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "backup": {"type": "str", "default": "latest"},
            "dest": {"type": "path", "default": "~/rxrbench/backups"},
            "tools": {"type": "dict", "default": {}},
            "chip": {"type": "str"},
            "socket": {"type": "path"},
            "settle": {"type": "float", "default": 90.0},
            "flasher": {"type": "str"},
            "route": {"type": "dict"},
            "run_dir": {"type": "path"},
            "chip_id": {"type": "str"},
            "uf2": {"type": "dict"},
            "enter": {"type": "list", "elements": "str"},
            "manual_wait": {"type": "float", "default": 0},
            "enter_wait": {"type": "float"},
            "ask_timeout": {"type": "float"},
            "app_manager_overwrite": {"type": "bool", "default": False},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    familyspec.load(p["families"])
    try:
        access.chip_restore(module, p, backup_path(p["dest"], p["board"], p["backup"]))
    except (common.FlashError, broker_client.BrokerError, stateline.StateError, OSError) as e:
        module.fail_json(msg=str(e), unanswered=isinstance(e, common.Unanswered))


if __name__ == "__main__":
    main()
