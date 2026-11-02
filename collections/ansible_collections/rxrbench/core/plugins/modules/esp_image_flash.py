#!/usr/bin/python
"""Put a whole ESP32 image (a merged .bin linked for 0x0) on a board, through its ROM loader."""
DOCUMENTATION = r"""
module: esp_image_flash
short_description: Write a whole ESP32 image (a merged .bin at 0x0) through the ROM loader
description:
  - "Writes a release's merged image, the file that brings its own bootloader, partition table,
    application and data partitions (and, staged into it, the images a release publishes for
    its other partitions), the two ways a vendor's web installer does."
  - "An update, for a board on the image's partition table whose apps differ: only the app
    partitions are written (C(chip_blocks.py region-update)), and the bootloader, the table and
    every data partition (the firmware's config and identity) are kept."
  - "A full install, for a first flash or with C(full_erase): the chip becomes the image and
    0xFF after it, every data partition as the image has it. Only the 64 KB blocks that differ
    are written (C(chip_blocks.py place)), in one ROM-loader session."
  - "Decides by content, through the loader's stub hashes: the image's partition table and app
    partitions against the chip's. A board whose table and apps match runs the image and is
    not written; a bootloader that is not the image's is reported (C(note)), not written,
    as the installer's update leaves it. Data partitions are not compared (the firmware writes
    them as it runs)."
  - "The chip must be the board: its MAC, read by the loader in the write's own session, must be
    the registry's (C(mac): C(policy.chip_mac), or a C(usb_serial) that is the MAC). Nothing is
    written without one."
  - "Refuses, before anything is written: an image that is not a C(raw-bin) merged image (a
    partition table at 0x8000, the bootloader's magic where the chip's ROM loads it), one for
    another type of board or another firmware than declared, one larger than the chip; a chip
    covering a region the layout does not own (other firmware, or data every app of a chip
    with an app manager shares) unless C(overwrite) names it (the write rule: a plan over the
    survey's record, C(facts_chip); the apply over the chip itself); and a chip whose
    partition table is not the image's, which runs other firmware, unless C(first_flash), and
    then the survey's security and PSRAM checks."
  - "A board with a serial broker has the broker release its port for the run and take it back
    after it."
  - Supports check mode and diff. A plan reads the chip too (a reset into its loader and back).
options:
  board:
    description: The board's name.
    type: str
    required: true
  image:
    description: The image directory (the file and its C(manifest.json)), on the bench host.
    type: path
    required: true
  chip:
    description: The board's chip, as esptool names it (C(bench_mcu)).
    type: str
    required: true
  psram:
    description: The PSRAM the board's type has; a first flash checks the survey saw it.
    type: str
  mac:
    description: The chip's MAC as the registry holds it (C(bench_chip_mac)).
    type: str
  board_tag:
    description: The board's type; the image must be for it.
    type: str
  declared:
    description: The board's C(firmware) block; the image must be what it declares.
    type: dict
  first_flash:
    description: The layout's go-ahead (C(bench_first_flash)) to write a chip that runs other firmware.
    type: bool
    default: false
  full_erase:
    description: A full install (C(bench_full_erase)) instead of an update, on a board that runs
      the image's partition table, whether its apps differ or not; its config and identity are
      gone after it. Asked of one run, not kept in a layout.
    type: bool
    default: false
  boot_radio:
    description: What the layout check found wrong with the image's own radio (catalog
      C(boot_radio)) against the site's RF rules; a fresh write (first flash, family switch) is
      refused while any is listed, unless C(boot_radio_ok).
    type: list
    elements: str
    default: []
  boot_radio_ok:
    description: The layout's word (C(bench_boot_radio_ok)) that the board may transmit on its
      image's own radio for a moment (a dummy load, a shielded box).
    type: bool
    default: false
  overwrite:
    description:
      - The regions a write may take although they hold firmware the board does not run
        (C(bench_overwrite)), by partition label, or C(all) (docs/ARCHITECTURE.md#partitions-and-the-write-rule).
    type: list
    elements: str
    default: []
  app_manager_overwrite:
    description: The layout's go-ahead (C(bench_app_manager_overwrite)) to write over an app manager and its apps.
    type: bool
    default: false
  facts_chip:
    description: >-
      The survey's record of the chip (its esptool probe: C(partitions) with the region
      inventory, C(gathered)). The checks before a write that read nothing from the chip (a
      plan's) run the write rule over it; the apply asks the chip itself.
    type: dict
    default: {}
  facts_tags:
    description: The board's survey tags (security state, PSRAM, an app manager).
    type: list
    elements: str
    default: []
  tools:
    description: Where the bench host keeps its tools. Keys C(esptool), C(python), C(chip_blocks), C(devreg).
    type: dict
    default: {}
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)); a plan needs none.
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
extends_documentation_fragment:
  - rxrbench.core.check_depth
"""

EXAMPLES = r"""
- name: The declared image, written whole
  rxrbench.core.esp_image_flash:
    board: "{{ bench_board }}"
    image: "{{ board_host_store }}/{{ bench_image | basename }}"
    chip: "{{ bench_mcu }}"
    mac: "{{ bench_chip_mac }}"
    first_flash: "{{ bench_first_flash | default(false) }}"
"""

RETURN = r"""
image:
  description: What the image is.
  returned: always
  type: dict
device:
  description: What the board runs after the run (in check mode, what it runs now).
  returned: success
  type: dict
chip:
  description: The chip as its loader reported it (mac, flash_size, chip, size).
  returned: success
  type: dict
why:
  description: Why a write is (or would be) needed, and whether it is an update or a full install.
  returned: when changed
  type: str
note:
  description: What the board holds that the image does not and that is not written (a bootloader).
  returned: when there is one
  type: str
timing:
  description: Each phase of the run (resolve, read, preflight, write, verify, leave) and its duration in seconds, as C(module_utils/timing.py) records it.
  returned: success
  type: list
  elements: dict
checked:
  description: The depth of the checks before the write (C(full) or C(spot)) and why (docs/ARCHITECTURE.md#checking-depth).
  returned: success
  type: dict
"""

import os

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, gate
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, espimage


WRITES = {"update": "update: the app partitions only; bootloader, table, config and identity kept",
          "full": "full install: the whole chip; the firmware's config and identity are gone"}


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "image": {"type": "path", "required": True},
            "chip": {"type": "str", "required": True},
            "psram": {"type": "str"},
            "mac": {"type": "str"},
            "board_tag": {"type": "str"},
            "declared": {"type": "dict"},
            "first_flash": {"type": "bool", "default": False},
            "check_depth": {"type": "str", "choices": ["full", "spot"], "default": "full"},
            "full_erase": {"type": "bool", "default": False},
            "boot_radio": {"type": "list", "elements": "str", "default": []},
            "boot_radio_ok": {"type": "bool", "default": False},
            "app_manager_overwrite": {"type": "bool", "default": False},
            "overwrite": {"type": "list", "elements": "str", "default": []},
            "facts_tags": {"type": "list", "elements": "str", "default": []},
            "facts_chip": {"type": "dict", "default": {}},
            "tools": {"type": "dict", "default": {}},
            "socket": {"type": "path"},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    flasher = None
    try:
        manifest, image_file = common.load_image(p["image"])
        tools = {k: v for k, v in p["tools"].items() if v}
        flasher = espimage.EspImageFlasher(
            p["board"], manifest, image_file, chip=p["chip"], psram=p["psram"],
            board_tag=p["board_tag"], declared=p["declared"], mac=p["mac"],
            first_flash=p["first_flash"], manager_overwrite=p["app_manager_overwrite"],
            overwrite=p["overwrite"], full_erase=p["full_erase"],
            facts_tags=p["facts_tags"], facts_chip=p["facts_chip"],
            tools={k: v for k, v in tools.items() if k in ("esptool", "python", "chip_blocks")},
            **({"devreg": tools["devreg"]} if "devreg" in tools else {}))
        # The port is the loader's for the run: a broker that holds it lets go and takes it back.
        sock = p["socket"] or broker_client.socket_path(p["board"])
        broker = broker_client.Board(p["board"], path=sock) if os.path.exists(sock) else None
        if broker:
            broker.__enter__()
            broker.release(ttl=2400)
        try:
            # the flasher takes the go-ahead itself (another firmware's table); drive is told
            # too, so a go-ahead the chip does not need is said
            r = common.drive(flasher, manifest, check_mode=module.check_mode, check_depth=p["check_depth"],
                             first_flash=p["first_flash"],
                             boot_radio=p["boot_radio"], boot_radio_ok=p["boot_radio_ok"])
        finally:
            if broker:
                try:
                    broker.reacquire()
                finally:
                    broker.__exit__(None, None, None)
    except (common.FlashError, broker_client.BrokerError, OSError) as e:
        module.fail_json(msg=str(e), unanswered=isinstance(e, common.Unanswered))
    common.stale_first_flash(module, p["board"], r)
    result = {"changed": r["changed"], "image": r["image"], "chip": flasher.chip_facts or {},
              "device": r["before"] if module.check_mode else r["after"]}
    if flasher.note:
        result["note"] = flasher.note
    result["timing"] = r.get("timing", [])
    result["checked"] = r.get("checked")
    result["identity_checked"] = r.get("identity_checked")
    if r["changed"]:
        result["why"] = r["why"]
        result["diff"] = {"before": {k: v for k, v in r["before"].items() if v is not None},
                          "after": {k: v for k, v in r["after"].items() if v is not None}}
        # what kind of write, where the plan shows it: an update keeps the config, a full
        # install does not
        mode = flasher.wrote or flasher.mode or ("full" if r.get("first") else None)
        result["diff"]["after"]["write"] = WRITES.get(mode, "the image")
    module.exit_json(**result)


if __name__ == "__main__":
    main()
