#!/usr/bin/python
"""Copy an ESP32 board's whole flash chip off it."""

DOCUMENTATION = r"""
module: chip_backup
short_description: Back up a board's whole flash chip
description:
  - "Which route reads the chip goes by the board's flasher: an ESP32 through its ROM loader
    (below), an RP2040 or RP2350 through its ROM bootloader (C(flash/picochip.py)), a chip
    behind a debug probe through the probe, checked by its unique id (C(flash/swdchip.py),
    flasher C(swd), C(route)), a simulated board's flash file (C(run_dir))."
  - "Reads every byte of an ESP32-S3 board's flash (bootloader, partition layout, both
    application slots, config, everything) into a compressed image on the bench host, with
    a record of the chip it came from (its factory MAC, read by the ROM loader) and of what
    the board said it ran before the read."
  - "This is the backup that does not depend on any firmware's flash map: it brings back a
    board whatever was written over it since, another firmware family or a stock one
    included. It holds the node's private keys: the directory is the bench user's only
    (0700), and C(chip_restore) writes it back to the same chip only."
  - "One ROM-loader session (C(chip_blocks.py) on the bench host): the chip hashes every 64 KB
    block, only blocks that are not erased are read, and each is checked against the chip's
    own hash; the board does not boot in between, so it cannot change its config in between.
    Then the board is reset and must come back. Nothing is written to it."
  - "What the board runs is read through its broker in the vocabulary of C(family) when one
    is given and it answers; a board that answers nothing (blank, or stock firmware with no
    state line) is still backed up, with the record saying so."
  - Each call makes a new backup (C(changed)). Nothing is read in check mode.
options:
  board:
    description: The board's name.
    type: str
    required: true
  family:
    description: The firmware family the board is expected to run, to record its identity.
    type: str
  dest:
    description: Where backups are kept on this host; this one goes to
      C(<dest>/<board>/chip/<stamp>/).
    type: path
    default: ~/rxrbench/backups
  chip:
    description: The board's chip, as esptool names it (C(bench_mcu), from the board's type in catalog/boards.yml).
    type: str
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
    description: Seconds the board has to come back after the read.
    type: float
    default: 60.0
  snapshot:
    description: "A name for this backup (C([A-Za-z0-9._-]+)): it is also filed as
      C(<dest>/<board>/snapshot/<name>), a configuration that C(chip_restore) puts back by
      name. A later snapshot of the same name replaces it."
    type: str
  families:
    description: The device families as data, the inventory's C(bench_families). It says how
      a board's identity is read and what an image of a family must match.
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Back the whole chip up
  rxrbench.core.chip_backup:
    board: "{{ bench_board }}"
    family: "{{ bench_family }}"
"""

RETURN = r"""
path:
  description: The backup's directory on the bench host.
  returned: when a backup was made
  type: str
chip:
  description: The chip as the ROM loader reported it (mac, flash_size, chip, features).
  returned: when a backup was made
  type: dict
sha256:
  description: SHA-256 of the uncompressed image.
  returned: when a backup was made
  type: str
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, stateline
from ansible_collections.rxrbench.core.plugins.module_utils.flash import access, common


def main():
    module = AnsibleModule(
        argument_spec={
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "family": {"type": "str"},
            "dest": {"type": "path", "default": "~/rxrbench/backups"},
            "tools": {"type": "dict", "default": {}},
            "chip": {"type": "str"},
            "socket": {"type": "path"},
            "settle": {"type": "float", "default": 60.0},
            "snapshot": {"type": "str"},
            "flasher": {"type": "str"},
            "route": {"type": "dict"},
            "run_dir": {"type": "path"},
            "chip_id": {"type": "str"},
            "uf2": {"type": "dict"},
            "enter": {"type": "list", "elements": "str"},
            "manual_wait": {"type": "float", "default": 0},
            "enter_wait": {"type": "float"},
            "ask_timeout": {"type": "float"},
        },
        supports_check_mode=True,
    )
    p = module.params
    familyspec.load(p["families"])
    try:
        access.chip_backup(module, p)
    except (common.FlashError, broker_client.BrokerError, stateline.StateError, OSError) as e:
        module.fail_json(msg=str(e), unanswered=isinstance(e, common.Unanswered))


if __name__ == "__main__":
    main()
