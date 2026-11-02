#!/usr/bin/python
"""Erase a board's whole flash chip, after reading that it is the registry's chip."""

DOCUMENTATION = r"""
module: chip_erase
short_description: Erase a board's whole flash chip (docs/ARCHITECTURE.md#chip-and-region-flows)
description:
  - "Erases every byte of the board's flash: bootloader, partition table, applications and
    config. The board then runs nothing of its own (an ESP32 or an RP2040 stays in its ROM
    loader, a chip behind a probe has nothing to run) until it is flashed again. One config
    region is C(region_erase)'s."
  - "The route goes by the board's flasher, as for C(chip_backup): an ESP32 through its ROM
    loader (C(esptool), C(esp-image)), an RP2040 or RP2350 through its ROM bootloader
    (C(picoboot)), a chip behind a debug probe (C(swd)), a simulated board's flash file."
  - "The chip is read and checked before anything is erased, in the same session as the erase:
    an ESP32 by the factory MAC its loader reads, which the registry must hold (C(mac)); an
    RP2040 by its chip id, which picotool selects it by; a chip behind a probe by its unique id,
    read in every probe session. Another chip is refused."
  - "Refused unless a chip backup of this very chip, by this route, is on the bench host
    (C(dest)), since an erase cannot be undone: the refusal says how to take one. A chip whose
    contents are not wanted is erased with C(without_backup), said explicitly."
  - "Success is the chip reading erased afterwards over its whole flash (the probe's hash, the
    ESP32 stub's block hashes, picotool's read back)."
  - With check mode the chip is identified (an ESP32 is reset into its loader and back) and
    nothing is erased; the plan says what would be erased and which backup of this chip there is.
options:
  board:
    description: The board's name.
    type: str
    required: true
  dest:
    description: Where backups are kept on this host (as for C(chip_backup)); a backup of this
      chip is looked for here.
    type: path
    default: ~/rxrbench/backups
  without_backup:
    description: Erase although no backup of this chip is on the bench host. The caller's word
      that the chip's contents are not wanted.
    type: bool
    default: false
  mac:
    description: An ESP32's factory MAC as the registry holds it (C(bench_chip_mac)). Required for
      an ESP32.
    type: str
  chip:
    description: The board's chip, as esptool names it (C(bench_mcu)).
    type: str
  tools:
    description: Where the bench host keeps its tools. Keys C(esptool), C(python), C(chip_blocks),
      C(devreg), C(picotool), C(swd_chip).
    type: dict
    default: {}
  flasher:
    description: How the board is flashed (C(bench_flasher)). A simulated board is told by I(run_dir).
    type: str
  route:
    description: The board's flash route as the registry gives it (C(bench_routes.flash)); for a
      chip behind a debug probe its C(probe), C(chip), C(uid) and C(nrst).
    type: dict
  chip_id:
    description: A picoboot board's chip id (the registry's C(usb_serial)).
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
    description: A simulated board's run directory (C(bench_sim_run)). Given, the board's chip is its flash file.
    type: path
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  settle:
    description: Seconds a board has to come back where it is waited for.
    type: float
    default: 90.0
  family:
    description: The board's declared family (C(bench_family)).
    type: str
  app_manager_overwrite:
    description: Unused here (an erase leaves no app manager to orphan); accepted for one call shape with C(chip_restore).
    type: bool
    default: false
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). A plan (check mode) needs none.
    type: bool
    default: false
  families:
    description: The device families as data, the inventory's C(bench_families).
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Erase the board's whole chip (a backup of it is on the bench host)
  rxrbench.core.chip_erase:
    board: "{{ bench_board }}"
    flasher: "{{ bench_flasher }}"
    mac: "{{ bench_chip_mac }}"
"""

RETURN = r"""
chip:
  description: The chip as read before the erase (its unique id, chip id or MAC, its flash size).
  returned: always
  type: dict
size:
  description: The whole flash, in bytes.
  returned: always
  type: int
backups:
  description: The chip backups of this very chip on the bench host, newest first.
  returned: always
  type: list
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, stateline
from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils.flash import access, common


def main():
    module = AnsibleModule(
        argument_spec={
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "dest": {"type": "path", "default": "~/rxrbench/backups"},
            "without_backup": {"type": "bool", "default": False},
            "mac": {"type": "str"},
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
            "family": {"type": "str"},
            "app_manager_overwrite": {"type": "bool", "default": False},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    familyspec.load(p["families"])
    try:
        access.chip_erase(module, p)
    except (common.FlashError, broker_client.BrokerError, stateline.StateError, OSError) as e:
        module.fail_json(msg=str(e), unanswered=isinstance(e, common.Unanswered))


if __name__ == "__main__":
    main()
