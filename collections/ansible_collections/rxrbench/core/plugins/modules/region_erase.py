#!/usr/bin/python
"""Erase one config region of a board, where its image's flash map or its declared ranges put it."""

DOCUMENTATION = r"""
module: region_erase
short_description: Erase one config region of a board (a wedged node's way back)
description:
  - "Erases one region of a board's flash with `esptool erase_region`: a config region of
    its firmware family (a file system, an NVS partition), whose offset and
    size come from the image's own flash map, never from a guess. A firmware that formats an
    erased region on its first mount (littlefs does) starts clean."
  - "It is the flash-side way to the same state a firmware wipe reaches, for a node whose
    firmware cannot do it: wedged, or absent. Refused: a region that is not a config region
    (an app slot, a bootloader); a board that answers but does not run the image (its map may
    differ); a board that does not answer, unless C(wedged) is declared; a non-ESP32 board (an
    nRF52840 behind a UF2 bootloader has no USB path for this: SWD, or the firmware's own wipe)."
  - "An C(esp-image) board (a release's merged image written whole) has its config regions in
    the image's own partition table. It needs no console: the loader proves the chip is the
    board (its MAC, C(mac)) and that its partition table is the image's, in the same session as
    the erase (C(flash/esp_image_regions.py)); C(wedged) does not apply."
  - "An RP2040 or RP2350 (C(picoboot)) has its config regions in its UF2 image's flash map; the
    region is erased through the ROM bootloader, after the board is shown to run that image, and
    read back erased."
  - "Nothing but the region named is erased: with none named the module refuses and lists the
    board's config regions. A whole chip is C(chip_erase)'s (C(just erase-chip))."
  - "A chip behind a debug probe (C(swd)) has its config regions in the ranges declared for it
    (C(flash_regions), C(role) C(config)). The probe's host tool erases the range (whole
    sectors) in a session guarded by the chip's unique id; the chip must hold the image that
    declares the ranges, unless C(wedged) is declared (C(flash/swd_regions.py))."
  - The board resets into its ROM loader and back; its broker releases the console meanwhile.
    Check mode makes every check and erases nothing.
options:
  board:
    description: The board's name.
    type: str
    required: true
  image:
    description: The image directory (C(manifest.json), C(flash.json)) of the firmware the board
      runs or is to run.
    type: path
    required: true
  region:
    description: The config region by name, one of the family's (C(storage), C(fs), ...).
      Required in effect - with none named nothing is erased, and the refusal lists them.
    type: str
  wedged:
    description: The board does not answer, and the caller says so. Without it a silent board is
      refused, since nothing then shows that the image's map is the board's.
    type: bool
    default: false
  overwrite:
    description:
      - The regions this write may take although other firmware keeps something there
        (C(bench_overwrite)), by partition label, or C(all). On a chip with an app manager every
        data region is shared by its apps (docs/ARCHITECTURE.md#partitions-and-the-write-rule).
    type: list
    elements: str
    default: []
  app_manager_overwrite:
    description: The layout's go-ahead (C(bench_app_manager_overwrite)) to erase a region on a
      chip whose partition table shows an app manager with images installed beside it, which the
      erase would orphan. Refused without it.
    type: bool
    default: false
  chip:
    description: The board's chip, as esptool names it (C(bench_mcu), from the board's type in catalog/boards.yml).
    type: str
  tools:
    description: Where the bench host keeps its tools. Keys C(esptool), C(devreg).
    type: dict
    default: {}
  mac:
    description: An C(esp-image) board's chip MAC (C(bench_chip_mac), the registry's C(policy.chip_mac)),
      which the loader must read before anything is read or written.
    type: str
  flasher:
    description: How the board is flashed (C(bench_flasher)). A simulated board is told by I(run_dir), not by this.
    type: str
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
    description: A simulated board's run directory (C(bench_sim_run)). Not used - a simulated
      board has no config regions of its own; its flash file is erased by C(chip_erase).
    type: path
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  settle:
    description: Seconds the board has to come back after the erase.
    type: float
    default: 60.0
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
  route:
    description: (swd) The board's flash route as the registry gives it (C(bench_routes.flash)),
      with its C(probe), C(chip) (the part, for openocd), C(uid) (the chip's unique id, checked in
      every session) and C(nrst) when the probe's reset line is wired.
    type: dict
  flash_regions:
    description: The ranges declared for a chip with no table of its own (C(bench_flash_regions),
      docs/ARCHITECTURE.md#partitions-and-the-write-rule). Those with C(role) C(config) are its config regions,
      at their absolute addresses; without any, the image's flash map says where they are.
    type: list
    elements: dict
    default: []
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Erase a wedged node's file system
  rxrbench.core.region_erase:
    board: "{{ bench_board }}"
    image: "{{ board_host_store }}/{{ bench_image | basename }}"
    wedged: true
"""

RETURN = r"""
region:
  description: What was (or in check mode would be) erased (name, offset, size).
  returned: success
  type: dict
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
            "image": {"type": "path", "required": True},
            "region": {"type": "str"},
            "wedged": {"type": "bool", "default": False},
            "app_manager_overwrite": {"type": "bool", "default": False},
            "overwrite": {"type": "list", "elements": "str", "default": []},
            "tools": {"type": "dict", "default": {}},
            "chip": {"type": "str"},
            "socket": {"type": "path"},
            "settle": {"type": "float", "default": 60.0},
            "flasher": {"type": "str"},
            "route": {"type": "dict"},
            "flash_regions": {"type": "list", "elements": "dict", "default": []},
            "mac": {"type": "str"},
            "run_dir": {"type": "path"},
            "chip_id": {"type": "str"},
            "uf2": {"type": "dict"},
            "enter": {"type": "list", "elements": "str"},
            "manual_wait": {"type": "float", "default": 0},
            "enter_wait": {"type": "float"},
            "ask_timeout": {"type": "float"},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    familyspec.load(p["families"])
    try:
        access.erase(module, p)
    except (common.FlashError, broker_client.BrokerError, stateline.StateError, OSError) as e:
        module.fail_json(msg=str(e), unanswered=isinstance(e, common.Unanswered))


if __name__ == "__main__":
    main()
