#!/usr/bin/python
"""Put an image on a microcontroller through an SWD probe."""

DOCUMENTATION = r"""
module: swd_flash
short_description: Flash a microcontroller through an SWD probe
description:
  - Writes a raw image to the flash of a chip wired to a debug probe (ST-Link, CMSIS-DAP) on
    the bench host, with C(openocd), through the host tool C(swd_chip.py).
  - A probe does not say what is wired to it. The probe is found by its registry name through
    C(devreg), and every session first reads the chip's own unique id and stops if it is not
    C(uid). Nothing is written without one.
  - Decides by content. What the device runs is the hash of its flash over the image's length;
    if that is the image's hash nothing is written, and after a write it must be.
  - An image staged from an Intel HEX or ELF file carries the address it goes at (C(base),
    tools/stage_raw.py); it is hashed and written from there, not from the start of flash.
  - "Refuses, before anything is written: an image that is not C(raw-bin), one for another type
    of board (C(board_tag)), one larger than the chip's flash, and one whose C(base) is outside
    the chip's flash or that runs past its end."
  - Supports check mode and diff.
options:
  board:
    description: The device's name.
    type: str
    required: true
  image:
    description: The image directory (the file and its C(manifest.json)), on this host.
    type: path
    required: true
  probe:
    description: The registry name of the debug probe the chip is wired to.
    type: str
    required: true
  chip:
    description: The part, for example C(STM32F103C8). It picks the debug recipe.
    type: str
    required: true
  uid:
    description: The chip's unique id, as the registry holds it.
    type: str
    required: true
  board_tag:
    description: The board's type, from the device registry. The image must be for it.
    type: str
  nrst:
    description: The probe's reset line is wired to the chip's reset pin. Sessions then connect
      with the chip held in reset, which reaches a chip whose firmware turned its debug clock off.
    type: bool
    default: false
  tools:
    description: Where the bench host keeps its tools. Keys C(swd_chip), C(devreg).
    type: dict
    default: {}
  flash_unknown:
    description: Write a board that answers as its family but reports neither a build nor
      a hash, so nothing can tell whether it already runs the image. On by default (a roll
      from old firmware needs it); a layout turns it off with C(bench_flash_unknown), and
      such a board is then refused.
    type: bool
    default: true
  flash_regions:
    description: The chip's declared ranges (C(bench_flash_regions), docs/ARCHITECTURE.md#partitions-and-the-write-rule).
      With them, the write rule reads each range's start through the probe before a
      write, and a write over the C(boot) range is refused unless C(overwrite) names it.
    type: list
    elements: dict
    default: []
  overwrite:
    description: The ranges a write may take although they hold other firmware or the
      bootloader (C(bench_overwrite)), by label, or C([all]).
    type: list
    elements: str
    default: []
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode) needs none.
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
extends_documentation_fragment:
  - rxrbench.core.check_depth
"""

EXAMPLES = r"""
- name: Flash through the probe
  rxrbench.core.swd_flash:
    board: "{{ bench_board }}"
    image: "{{ board_host_store }}/{{ bench_image | basename }}"
    probe: "{{ bench_routes.flash.probe }}"
    chip: "{{ bench_routes.flash.chip }}"
    uid: "{{ bench_routes.flash.uid }}"
    board_tag: "{{ bench_board_tag }}"
"""

RETURN = r"""
image:
  description: What the image is.
  returned: always
  type: dict
device:
  description: What the chip holds after the run (in check mode, what it holds now).
  returned: success
  type: dict
chip:
  description: What the chip says it is (unique id, id code, flash size, supply voltage).
  returned: success
  type: dict
why:
  description: Why a flash is (or would be) needed.
  returned: when changed
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

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, swd
from ansible_collections.rxrbench.core.plugins.module_utils import gate


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "image": {"type": "path", "required": True},
            "probe": {"type": "str", "required": True},
            "chip": {"type": "str", "required": True},
            "uid": {"type": "str", "required": True},
            "board_tag": {"type": "str"},
            "nrst": {"type": "bool", "default": False},
            "tools": {"type": "dict", "default": {}},
            "flash_unknown": {"type": "bool", "default": True},
            "check_depth": {"type": "str", "choices": ["full", "spot"], "default": "full"},
            "flash_regions": {"type": "list", "elements": "dict", "default": []},
            "overwrite": {"type": "list", "elements": "str", "default": []},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    try:
        manifest, image_file = common.load_image(p["image"])
        tools = {k: v for k, v in p["tools"].items() if v and k in ("swd_chip", "devreg")}
        flasher = swd.SwdFlasher(p["board"], manifest, image_file, p["probe"], p["chip"],
                                 p["uid"], board_tag=p["board_tag"], nrst=p["nrst"],
                                 **tools)
        flasher.flash_regions, flasher.overwrite = p["flash_regions"], tuple(p["overwrite"])
        r = common.drive(flasher, manifest, check_mode=module.check_mode, check_depth=p["check_depth"],
                             flash_unknown=p["flash_unknown"])
    except (common.FlashError, OSError) as e:
        # `unanswered`: the chip is not there to read (the play skips it, not the bench)
        module.fail_json(msg=str(e), unanswered=isinstance(e, common.Unanswered))
    facts = {k: v for k, v in (flasher.chip_facts or {}).items() if k not in ("sha256", "length")}
    result = {"changed": r["changed"], "image": r["image"], "chip": facts,
              "device": r["before"] if module.check_mode else r["after"]}
    result["timing"] = r.get("timing", [])
    result["checked"] = r.get("checked")
    result["identity_checked"] = r.get("identity_checked")
    if r["changed"]:
        result["why"] = r["why"]
        result["diff"] = {"before": {k: v for k, v in r["before"].items() if v is not None},
                          "after": {k: v for k, v in r["after"].items() if v is not None}}
    module.exit_json(**result)


if __name__ == "__main__":
    main()
