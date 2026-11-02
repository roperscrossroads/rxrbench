#!/usr/bin/python
"""Put an image on a board over its wired route."""

DOCUMENTATION = r"""
module: board_flash
short_description: Flash a board over its wired route
description:
  - Puts an image on a board whose console a serial broker owns. The broker releases the
    console for the write and takes it back afterwards.
  - Decides from identities, not from tool output. If the board already runs the image,
    nothing is written. After a write the board is read again and must run the image.
  - "Refuses, before the console is released, an image that is not for this type of board
    (C(board_tag)) or not what the layout declares for it (C(declared))."
  - An image is a directory holding the image file and a C(manifest.json) that says what it is.
  - "Flashers: C(sim) (a simulated board), C(esptool) (ESP32-S3, the application slot only)
    and C(uf2) (nRF52840 with a UF2 bootloader, through the bench host's UF2 tool)."
  - C(esptool) and C(uf2) run on the bench host the board is plugged into, resolve and guard
    its port through C(devreg), and need the image directory on that host.
  - Supports check mode and diff.
options:
  board:
    description: The board's name.
    type: str
    required: true
  image:
    description: The image directory (the file and its C(manifest.json)), on the host this
      module runs on.
    type: path
    required: true
  flasher:
    description: How the image is written.
    type: str
    choices: [sim, esptool, uf2]
    required: true
  chip:
    description: The board's chip, as esptool names it (C(bench_mcu), from the board's type in catalog/boards.yml).
    type: str
  psram:
    description: The PSRAM the board's type has (C(bench_psram), e.g. C(2MB)); a first flash checks the chip reports it.
    type: str
  board_tag:
    description: The board's type, from the device registry. The image must be for it.
    type: str
  declared:
    description: "The board's C(firmware) block, as the layout declares it. The family says what the
      image must match (a class and features, a role: the family says which); a mismatch is a refusal."
    type: dict
  run_dir:
    description: C(sim) only. The simulated bench's run directory.
    type: path
  tools:
    description: Where the bench host keeps its tools. Keys C(esptool), C(uf2_tool), C(devreg),
      C(imginfo); each defaults to the bench hosts' standing location.
    type: dict
    default: {}
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  deadline:
    description: Seconds allowed for each shell command.
    type: float
    default: 10.0
  settle:
    description: Seconds the board has to come back after the write and answer on its
      shell. A board whose console is also its log sink can flood it for over a minute
      after a reboot (a node rejoining its network), during which the shell's echo is
      mangled and the board reads as not answering; 60 s was too short for that.
    type: float
    default: 180.0
  first_flash:
    description: Allow writing a board that does not answer as the image's family (stock, blank,
      other). Off by default; a layout turns it on per board (C(bench_first_flash)). The
      flasher's own first-flash refusals still apply. Declared for a board that answers as its
      family, it is left over from an earlier first flash, and the module warns.
    type: bool
    default: false
  boot_switch:
    description: C(just boot) replaced the board's loader for this family (its facts'
      C(boot_switch) probe), so the app it ran no longer boots. A first flash, as
      C(first_flash) allows, without its warning.
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
  facts_chip:
    description: >-
      The survey's record of the chip (its esptool probe: C(partitions) with the region
      inventory, C(gathered)). The checks before a write that read nothing from the chip (a
      plan's) run the write rule over it; the apply asks the chip itself.
    type: dict
    default: {}
  facts_tags:
    description: The board's survey tags; an ESP32 first flash needs its security state and
      PSRAM size from them.
    type: list
    elements: str
    default: []
  bootloader_version:
    description: The board's known bootloader version; an nRF52840 first flash needs the bootloader its board type requires.
    type: str
  uf2_enter_as:
    description: For a UF2 board, the family whose way of asking for UF2 mode its app takes
      when no family's read can say what it runs (C(bench_uf2_enter_as),
      docs/ARCHITECTURE.md#entering-uf2-mode). Refused when the board says it runs another.
    type: str
  flash_unknown:
    description: Write a board that answers as its family but reports neither a build nor
      a hash, so nothing can tell whether it already runs the image. On by default (a roll
      from old firmware needs it); a layout turns it off with C(bench_flash_unknown), and
      such a board is then refused.
    type: bool
    default: true
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode) needs none.
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
    description: The layout's go-ahead (C(bench_app_manager_overwrite)) to write an ESP32 whose
      partition table shows an app manager with images installed beside it, which the write
      (the app slot, or the whole chip on a first flash) would orphan; the same as C(overwrite) C(all).
      A plan asks the write rule of the survey's record (C(facts_chip)), the apply asks the
      chip itself, before anything is written.
    type: bool
    default: false
  families:
    description: The device families as data, the inventory's C(bench_families). It says how
      a board's identity is read and what an image of a family must match.
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
extends_documentation_fragment:
  - rxrbench.core.check_depth
"""

EXAMPLES = r"""
- name: Flash the declared image
  rxrbench.core.board_flash:
    board: "{{ bench_board }}"
    image: "{{ bench_image }}"
    flasher: sim
    board_tag: "{{ bench_board_tag }}"
    declared: "{{ firmware }}"
    run_dir: "{{ bench_sim_run }}"
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

import os

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, stateline
from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils.flash import (common, esptool, sim,
                                                                         uf2)
from ansible_collections.rxrbench.core.plugins.module_utils.flash import chip as chip_tools


def main():
    module = AnsibleModule(
        argument_spec={
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "image": {"type": "path", "required": True},
            "flasher": {"type": "str", "required": True, "choices": ["sim", "esptool", "uf2"]},
            "tools": {"type": "dict", "default": {}},
            "board_tag": {"type": "str"},
            "declared": {"type": "dict"},
            "chip": {"type": "str"},
            "psram": {"type": "str"},
            "run_dir": {"type": "path"},
            "socket": {"type": "path"},
            "deadline": {"type": "float", "default": 10.0},
            "settle": {"type": "float", "default": 180.0},
            "first_flash": {"type": "bool", "default": False},
            "boot_switch": {"type": "bool", "default": False},
            "boot_radio": {"type": "list", "elements": "str", "default": []},
            "boot_radio_ok": {"type": "bool", "default": False},
            "facts_tags": {"type": "list", "elements": "str", "default": []},
            "facts_chip": {"type": "dict", "default": {}},
            "bootloader_version": {"type": "str"},
            "uf2_enter_as": {"type": "str"},
            "flash_unknown": {"type": "bool", "default": True},
            "check_depth": {"type": "str", "choices": ["full", "spot"], "default": "full"},
            "app_manager_overwrite": {"type": "bool", "default": False},
            "overwrite": {"type": "list", "elements": "str", "default": []},
            **gate.ARGS,
        },
        required_if=[("flasher", "sim", ["run_dir"])],
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    familyspec.load(p["families"])
    try:
        manifest, image_file = common.load_image(p["image"])
        sock = p["socket"] or broker_client.socket_path(p["board"])
        # A board with no broker (its family has no console, or its broker is not started) is
        # asked on its port itself: what it runs is read either way.
        handle = broker_client.Board if os.path.exists(sock) else common.PortConsole
        with handle(p["board"], path=sock, timeout=p["settle"] + 60) as b:
            common_args = {"board_tag": p["board_tag"], "declared": p["declared"],
                           "deadline": p["deadline"], "settle": p["settle"]}
            tools = {k: v for k, v in p["tools"].items() if v}
            if p["flasher"] == "sim":
                flasher = sim.SimFlasher(b, p["board"], p["run_dir"], manifest, image_file,
                                         **common_args)
            elif p["flasher"] == "esptool":
                flasher = esptool.EsptoolFlasher(
                    b, p["board"], manifest, image_file, **common_args,
                    chip=p["chip"], psram=p["psram"], facts_tags=p["facts_tags"],
                    manager_overwrite=p["app_manager_overwrite"], facts_chip=p["facts_chip"],
                    **{k: tools[k] for k in ("esptool", "devreg", "imginfo") if k in tools})
                flasher.blocks_tools = chip_tools.tools_of(tools, p["chip"])
                flasher.overwrite = tuple(p["overwrite"])
            else:
                flasher = uf2.Uf2Flasher(
                    b, p["board"], manifest, image_file, **common_args,
                    bootloader_version=p["bootloader_version"], enter_as=p["uf2_enter_as"],
                    **{k: tools[k] for k in ("uf2_tool", "devreg") if k in tools})
            r = common.drive(flasher, manifest, check_mode=module.check_mode, check_depth=p["check_depth"],
                             flash_unknown=p["flash_unknown"],
                             first_flash=p["first_flash"] or p["boot_switch"],
                             boot_radio=p["boot_radio"],
                             boot_radio_ok=p["boot_radio_ok"])
    except (common.FlashError, broker_client.BrokerError, stateline.StateError) as e:
        # `unanswered`: the device is not there to read (the play skips it, not the bench)
        module.fail_json(msg=str(e), unanswered=isinstance(e, common.Unanswered))
    result = {"changed": r["changed"], "image": r["image"],
              "device": r["before"] if module.check_mode else r["after"]}
    # A family switch or a first flash: the board's config is another firmware's until it
    # is written, so a plan cannot read them (roles/board skips them in check mode).
    result["family_switch"] = bool(r.get("switch"))
    result["first_flash"] = bool(r.get("first"))
    if p["first_flash"]:
        common.stale_first_flash(module, p["board"], r)
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
