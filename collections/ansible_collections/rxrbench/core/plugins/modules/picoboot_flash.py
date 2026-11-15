#!/usr/bin/python
"""Put an image on a board through its chip's ROM bootloader (RP2040, RP2350), with picotool."""
DOCUMENTATION = r"""
module: picoboot_flash
short_description: Flash an RP2040 or RP2350 board through its ROM bootloader
description:
  - "Writes a UF2 image with C(picotool) through the chip's ROM bootloader (PICOBOOT), whatever
    firmware the board runs: the bootloader is in ROM and the same under every firmware."
  - The chip is selected by its flash unique id, the registry's C(usb_serial), and every session
    reads the id back and stops if it differs. Nothing is written without one.
  - Decides by content. What the board runs is the hash of its flash over exactly the addresses
    the image writes; if that is the image's hash nothing is written, and after a write it must
    be. So a plan reads the board too, which needs it in its bootloader for a moment.
  - The board is brought into its bootloader by the first of C(enter) that works
    (C(reset-interface), C(touch-1200), C(shell:<command>), C(manual)) and, when this run put it
    there, rebooted into its application at the end, in check mode too.
  - "Refuses, before anything is written: an image that is not C(uf2), one for another type of
    board (C(board_tag)), one of another UF2 family than the board type's, and one that writes
    outside the board type's flash region."
  - "A board with a serial broker (its firmware has a console) has the broker release its port
    for the run, so the ways in can use it, and take it back at the end."
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
  chip_id:
    description: The chip's flash unique id (16 hex), as the registry holds it in C(match.usb_serial).
    type: str
    required: true
  uf2:
    description: "The board type's C(uf2) block (catalog boards.yml): C(family) (an id or a
      list), C(app) (the flash region an image may write, C([lo, hi])), C(enter) (the ways
      into the bootloader, in order)."
    type: dict
    required: true
  board_tag:
    description: The board's type, from the device registry. The image must be for it.
    type: str
  declared:
    description: The board's C(firmware) block; the image must be what it declares.
    type: dict
  enter:
    description: The ways into the bootloader, in order, overriding the board type's.
    type: list
    elements: str
  manual_wait:
    description: Seconds to wait for someone to hold BOOTSEL and press reset, for C(manual).
    type: float
    default: 0
  enter_wait:
    description: Seconds to wait for the bootloader after each way in, over the board type's
      C(uf2.enter_wait) (default 10).
    type: float
  ask_timeout:
    description: Seconds a touch or a shell command may take before that way counts as failed,
      over the board type's C(uf2.ask_timeout) (default 15).
    type: float
  socket:
    description: The board's broker socket, when not the default; without a broker there the
      port is used as it is.
    type: path
  tools:
    description: Where the bench host keeps its tools. Keys C(picotool), C(devreg).
    type: dict
    default: {}
  console_expected:
    description: The bench reaches the board through its console (C(bench_via) is console), so an
      image whose console is elsewhere (the manifest's C(console), from its build) is refused.
    type: bool
    default: false
  console_elsewhere:
    description: The layout's word (C(bench_console_elsewhere)) that the board's console is meant
      to be elsewhere (a UART wired to the bench host).
    type: bool
    default: false
  flash_unknown:
    description: Kept for the same interface as the other flashers; a board read by its bytes
      always says what it holds.
    type: bool
    default: true
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module
      refuses to write; a plan (check mode) needs none.
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
extends_documentation_fragment:
  - rxrbench.core.check_depth
"""
EXAMPLES = r"""
- name: Flash through the ROM bootloader
  rxrbench.core.picoboot_flash:
    board: "{{ bench_board }}"
    image: "{{ board_host_store }}/{{ bench_image | basename }}"
    chip_id: "{{ bench_usb_serial }}"
    uf2: "{{ bench_board_type.uf2 }}"
    board_tag: "{{ bench_board_tag }}"
"""
RETURN = r"""
image:
  description: What the image is.
  returned: always
  type: dict
device:
  description: What the board holds after the run (in check mode, what it holds now).
  returned: success
  type: dict
chip:
  description: What the bootloader says the chip is (flash id, type, revision, flash size).
  returned: success
  type: dict
entered:
  description: How the board was brought into its bootloader (C(already) when it was there).
  returned: success
  type: str
why:
  description: Why a flash is (or would be) needed.
  returned: when changed
  type: str
timing:
  description: Each phase of the run (resolve, read, preflight, write, verify, leave) and its duration in seconds, as C(module_utils/timing.py) records it.
  returned: success
  type: list
  elements: dict
flash_id:
  description: >-
    The chip's flash id, read after the write, for a board the registry knew only by its ROM
    bootloader's serial (a blank RP2040). Its applications give it as their USB serial; the
    registry should match the board by it.
  returned: when the board was selected by its ROM serial and its flash id could be read
  type: str
checked:
  description: The depth of the checks before the write (C(full) or C(spot)) and why (docs/ARCHITECTURE.md#checking-depth).
  returned: success
  type: dict
"""
from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, picoboot
import os

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, gate


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "image": {"type": "path", "required": True},
            "chip_id": {"type": "str", "required": True},
            "uf2": {"type": "dict", "required": True},
            "board_tag": {"type": "str"},
            "declared": {"type": "dict"},
            "console_expected": {"type": "bool", "default": False},
            "console_elsewhere": {"type": "bool", "default": False},
            "enter": {"type": "list", "elements": "str"},
            "manual_wait": {"type": "float", "default": 0},
            "enter_wait": {"type": "float"},
            "ask_timeout": {"type": "float"},
            "socket": {"type": "path"},
            "tools": {"type": "dict", "default": {}},
            "flash_unknown": {"type": "bool", "default": True},
            "check_depth": {"type": "str", "choices": ["full", "spot"], "default": "full"},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    flasher = None
    try:
        manifest, image_file = common.load_image(p["image"])
        tools = {k: v for k, v in p["tools"].items() if v and k in ("picotool", "devreg")}
        flasher = picoboot.PicobootFlasher(
            p["board"], manifest, image_file, p["chip_id"], p["uf2"], board_tag=p["board_tag"],
            declared=p["declared"], enter=p["enter"], manual_wait=p["manual_wait"],
            enter_wait=p["enter_wait"], ask_timeout=p["ask_timeout"],
            console_expected=p["console_expected"], console_elsewhere=p["console_elsewhere"],
            **tools)
        # drive() reboots the board into its application at the end (flasher.leave), while the
        # board is still marked busy; this is for a failure before drive() got that far
        entered_by = None
        # The board's console is its broker's: let go of it for the run (the ways in open the
        # port), take it back at the end. No broker, nothing to release.
        sock = p["socket"] or broker_client.socket_path(p["board"])
        broker = broker_client.Board(p["board"], path=sock) if os.path.exists(sock) else None
        if broker:
            broker.__enter__()
            broker.release(ttl=1200)
        try:
            r = common.drive(flasher, manifest, check_mode=module.check_mode, check_depth=p["check_depth"],
                             flash_unknown=p["flash_unknown"])
            entered_by = flasher.last_entered or "already"
        finally:
            flasher.leave()
            if broker:
                try:
                    broker.reacquire()
                finally:
                    broker.__exit__(None, None, None)
    except (common.FlashError, picoboot.Uf2Error, broker_client.BrokerError, OSError) as e:
        module.fail_json(msg=str(e), unanswered=isinstance(e, common.Unanswered))
    result = {"changed": r["changed"], "image": r["image"], "chip": flasher.chip_facts or {},
              "entered": entered_by,
              "device": r["before"] if module.check_mode else r["after"]}
    result["timing"] = r.get("timing", [])
    result["checked"] = r.get("checked")
    result["identity_checked"] = r.get("identity_checked")
    if flasher.rom_serial and flasher.flash_id:
        # known by its ROM serial until now: the id its applications will give as their USB serial
        result["flash_id"] = flasher.flash_id
        result["msg"] = (f"{p['board']}: written, selected by its ROM serial {flasher.rom_serial}; "
                         f"its flash id is {flasher.flash_id}: record it in the registry as the "
                         f"board's match.usb_serial (the ROM serial under match_also)")
    if r["changed"]:
        result["why"] = r["why"]
        result["diff"] = {"before": {k: v for k, v in r["before"].items() if v is not None},
                          "after": {k: v for k, v in r["after"].items() if v is not None}}
    module.exit_json(**result)


if __name__ == "__main__":
    main()
