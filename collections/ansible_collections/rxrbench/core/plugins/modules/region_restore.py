#!/usr/bin/python
"""Write a board's config regions back from a backup."""

DOCUMENTATION = r"""
module: region_restore
short_description: Restore an ESP32 board's config regions from a region_backup
description:
  - "Writes the config regions saved by C(region_backup) (C(storage), and the bulk store
    where the backup has one) back onto the board they were read from. These bytes hold the
    node's identity key, its Bluetooth bonds, its channels and keys, and its configuration:
    the board comes back as the node it was when the backup was taken."
  - "A chip behind a debug probe (C(swd)) is written through the probe, only from a backup read
    from the same chip (its unique id), on a chip that holds the image declaring the ranges; a
    region that already holds the backup's bytes is left alone, and each one written must hash
    to the backup afterwards (C(flash/swd_regions.py))."
  - "An C(esp-image) board (a release's merged image written whole, a stock firmware's) is
    read through its loader, whatever console it has: its family's data partitions
    from the image's own partition table, the chip proven to be the board (C(mac)) and to run
    the image in the same session (C(flash/esp_image_regions.py))."
  - "Refused before anything is written: a backup of another board (its identity key would
    make two nodes with one id); a backup read from another chip (every backup records its
    chip: the MAC on an ESP32, the chip id on an RP2040; an ESP32's is read from the loader
    just before the write); a file whose size or SHA-256 is not the one recorded when it
    was read; a board that does not run the image whose flash map is given, or a map that
    does not put every region where the backup read it."
  - "A backup taken before backups recorded their chip is tied to its board's name alone; the
    result says so (C(chip_checked) false)."
  - "The backup may come from another build than the board runs now, as long as the map
    agrees: the config is key/value records the firmware reads on boot. The result says
    which build took it."
  - Writing flash resets the board into its ROM loader and back; the board must come back
    on the same image. The application is not touched.
  - It writes every time it is run (it does not read the board's regions first, which
    would cost another reset). In check mode it makes every check and writes nothing.
options:
  board:
    description: The board's name.
    type: str
    required: true
  backup:
    description: C(latest) for the newest backup of this board under C(dest), a backup's name
      (one of this board's under C(dest)), or a directory on this host.
    type: str
    default: latest
  overwrite:
    description:
      - The regions this write may take although other firmware keeps something there
        (C(bench_overwrite)), by partition label, or C(all). On a chip with an app manager every
        data region is shared by its apps (docs/ARCHITECTURE.md#partitions-and-the-write-rule).
    type: list
    elements: str
    default: []
  app_manager_overwrite:
    description: The layout's go-ahead (C(bench_app_manager_overwrite)) to write regions of a
      chip whose partition table shows an app manager with images installed beside it. Refused
      without it.
    type: bool
    default: false
  image:
    description: The image directory (C(manifest.json), C(flash.json)) of the build the
      board runs, on this host.
    type: path
    required: true
  dest:
    description: Where backups are kept on this host (as for C(region_backup)).
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
    description: Seconds the board has to come back after the write.
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
- name: Put the board's newest backup back
  rxrbench.core.region_restore:
    board: "{{ bench_board }}"
    image: "{{ board_host_store }}/{{ bench_image | basename }}"
"""

RETURN = r"""
path:
  description: The backup that was (or in check mode would be) written.
  returned: always, once a backup is found
  type: str
taken_by:
  description: The identity the board reported when the backup was taken.
  returned: always, once a backup is found
  type: dict
table_of:
  description: For a backup that knew the board only by its partition table (a whole merged
    image, C(esp-image)), the image whose table matched (image, hash, version); else null.
  returned: always, once a backup is found
  type: dict
regions:
  description: What was written (name, offset, size, sha256).
  returned: always, once a backup is found
  type: list
  elements: dict
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
            "backup": {"type": "str", "default": "latest"},
            "image": {"type": "path", "required": True},
            "dest": {"type": "path", "default": "~/rxrbench/backups"},
            "app_manager_overwrite": {"type": "bool", "default": False},
            "overwrite": {"type": "list", "elements": "str", "default": []},
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
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    familyspec.load(p["families"])
    try:
        access.restore(module, p)
    except (common.FlashError, broker_client.BrokerError, stateline.StateError, OSError) as e:
        module.fail_json(msg=str(e))


if __name__ == "__main__":
    main()
