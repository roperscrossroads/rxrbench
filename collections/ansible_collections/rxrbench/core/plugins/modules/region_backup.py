#!/usr/bin/python
"""Copy a board's config regions off it."""

DOCUMENTATION = r"""
module: region_backup
short_description: Back up an ESP32 board's config regions
description:
  - Reads the config regions of an ESP32-S3 board (C(storage), and the bulk store where
    the image has one) into a directory on the bench host, with a record of what was read,
    from where, and off which build. These bytes hold the node's identity key, its Bluetooth
    bonds, its channels and keys, and its configuration.
  - "A chip behind a debug probe (C(swd)) is read through the probe: its config regions are the
    ranges declared for it (C(flash_regions), C(role) C(config)), on a chip that holds the image
    declaring them; the backup records the chip's unique id (C(flash/swd_regions.py))."
  - "An C(esp-image) board (a release's merged image written whole, a stock firmware's) is
    read through its loader, whatever console it has: its family's data partitions
    from the image's own partition table, the chip proven to be the board (C(mac)) and to
    carry that table in the same session (C(flash/esp_image_regions.py)). The table is what says
    where the config regions are, so a board one release behind its layout can be backed up before
    its upgrade; the record says only the table was compared (C(matched) C(table)). A restore
    needs the whole image."
  - "The flash map is never guessed. It comes from the C(flash.json) of an image directory,
    which the build derived from its own devicetree, and the module refuses unless the board
    runs that image: a map from another build may put the config regions somewhere else, and a
    backup read from the wrong offset is a file of the wrong bytes with the right name."
  - Reading flash resets the board into its ROM loader and back, so the board reboots once;
    every region is read in that one loader session, so the regions are of one moment. Nothing
    is written to it. The board's serial broker releases the console for the read.
  - Each call makes a new backup; it reports C(changed) because a file is created on the
    host. It does nothing in check mode.
options:
  board:
    description: The board's name.
    type: str
    required: true
  image:
    description: The image directory (C(manifest.json), C(flash.json)) of the build the
      board runs, on this host.
    type: path
    required: true
  dest:
    description: Where backups are kept on this host. One directory per board and backup.
    type: path
    default: ~/rxrbench/backups
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
    description: How the board is written. C(picoboot) (an RP2040 or RP2350) reads and writes the
      regions through its ROM bootloader with picotool; anything else is an ESP32 through esptool.
    type: str
  chip_id:
    description: (picoboot) The chip's id (the registry's C(usb_serial)), which picotool finds it by.
    type: str
  uf2:
    description: (picoboot) The board type's C(uf2) block (its flash, its ways into the bootloader).
    type: dict
  enter:
    description: (picoboot) The board's ways into its bootloader, over the type's.
    type: list
    elements: str
  manual_wait:
    description: (picoboot) Seconds the C(manual) way waits for BOOTSEL.
    type: float
    default: 0
  enter_wait:
    description: (picoboot) Seconds to wait for the bootloader after each way in.
    type: float
  ask_timeout:
    description: (picoboot) Seconds a touch or shell command may take.
    type: float
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  settle:
    description: Seconds the board has to come back after the read.
    type: float
    default: 60.0
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
- name: Back the board's config regions up
  rxrbench.core.region_backup:
    board: "{{ bench_board }}"
    image: "{{ board_host_store }}/{{ bench_image | basename }}"
"""

RETURN = r"""
path:
  description: The backup's directory on the bench host.
  returned: when a backup was made
  type: str
regions:
  description: What was read (name, offset, size, sha256).
  returned: when a backup was made
  type: list
  elements: dict
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
            "image": {"type": "path", "required": True},
            "dest": {"type": "path", "default": "~/rxrbench/backups"},
            "tools": {"type": "dict", "default": {}},
            "chip": {"type": "str"},
            "flasher": {"type": "str"},
            "route": {"type": "dict"},
            "flash_regions": {"type": "list", "elements": "dict", "default": []},
            "mac": {"type": "str"},
            "chip_id": {"type": "str"},
            "uf2": {"type": "dict"},
            "enter": {"type": "list", "elements": "str"},
            "manual_wait": {"type": "float", "default": 0},
            "enter_wait": {"type": "float"},
            "ask_timeout": {"type": "float"},
            "socket": {"type": "path"},
            "settle": {"type": "float", "default": 60.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    familyspec.load(p["families"])
    try:
        access.backup(module, p)
    except (common.FlashError, broker_client.BrokerError, stateline.StateError, OSError) as e:
        module.fail_json(msg=str(e))


if __name__ == "__main__":
    main()
