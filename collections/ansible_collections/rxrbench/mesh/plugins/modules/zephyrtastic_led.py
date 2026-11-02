#!/usr/bin/python
"""The bench LED of a zephyrtastic board: what it is for (a colour) and whether it is in use."""

DOCUMENTATION = r"""
module: zephyrtastic_led
short_description: A zephyrtastic board's bench LED, as declared; or blink it to find it
description:
  - "Reads C(meshtastic led) and writes C(meshtastic led mode) / C(meshtastic led color) where
    they differ from what is declared. The colour says what the board is for (an RGB board
    shows it: idle blinks it, in-use holds it, locate blinks white); a one-LED board stores it
    and shows the patterns."
  - "With C(locate), blinks the board for that many seconds and changes nothing (which
    board is it?)."
  - "Firmware without a bench LED (C(meshtastic led) unknown), or without colours (an older
    image), is reported, not failed: the LED is declared state the next flash brings."
  - Supports check mode (the plan).
options:
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode), a read and a board that already matches need none.
    type: bool
    default: false
  board:
    description: The board's name.
    type: str
    required: true
  mode:
    description: C(in-use), C(idle) or C(off).
    type: str
    choices: [in-use, idle, "off"]
  color:
    description: What the board is for, as a colour.
    type: str
    choices: [default, red, green, blue, yellow, cyan, magenta]
  locate:
    description: Blink the board for this many seconds (1-600) instead; 0 stops a blink.
    type: int
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: The bench LED
  rxrbench.mesh.zephyrtastic_led:
    board: "{{ bench_board }}"
    mode: in-use
    color: magenta
"""

RETURN = r"""
led:
  description: The LED as read (mode, color; color is None on firmware without colours).
  returned: when the firmware has a bench LED
  type: dict
supported:
  description: Whether the firmware has a bench LED.
  returned: always
  type: bool
"""

import re

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client


def parse(output):
    """{mode, color} from `meshtastic led`, or None when the command is not there."""
    m = re.search(r"^mode:\s*(\S+)", output, re.M)
    if not m:
        return None
    c = re.search(r"\bcolor:\s*(\S+)", output)
    return {"mode": m.group(1), "color": c.group(1) if c else None}


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "board": {"type": "str", "required": True},
            "mode": {"type": "str", "choices": ["in-use", "idle", "off"]},
            "color": {"type": "str", "choices": ["default", "red", "green", "blue", "yellow",
                                                 "cyan", "magenta"]},
            "locate": {"type": "int"},
            "socket": {"type": "path"},
        },
        supports_check_mode=True,
    )
    p = module.params
    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            led = parse(b.exec("meshtastic led"))
            if led is None:
                module.exit_json(changed=False, supported=False,
                                 msg=f"{p['board']}: this firmware has no bench LED")
            if p["locate"] is not None:
                if not 0 <= p["locate"] <= 600:
                    module.fail_json(msg="locate is 0..600 seconds")
                if not module.check_mode:
                    b.exec(f"meshtastic led locate {p['locate']}")
                module.exit_json(changed=False, supported=True, led=led,
                                 msg=f"{p['board']}: blinking for {p['locate']} s")
            steps, notes = [], []
            if p["mode"] and led["mode"] != p["mode"]:
                steps.append(f"meshtastic led mode {p['mode']}")
            if p["color"]:
                if led["color"] is None:
                    notes.append("this firmware has no LED colours yet")
                elif led["color"] != p["color"]:
                    steps.append(f"meshtastic led color {p['color']}")
            if steps and not module.check_mode:
                gate.check(module)   # the layout's go-ahead, before the first write
                for cmd in steps:
                    b.exec(cmd)
                after = parse(b.exec("meshtastic led"))
                if after is None or (p["mode"] and after["mode"] != p["mode"]) or (
                        p["color"] and after["color"] not in (None, p["color"])):
                    module.fail_json(msg=f"{p['board']}: the LED is {after}, not as declared")
                led = after
    except broker_client.BrokerError as e:
        module.fail_json(msg=str(e))
    module.exit_json(changed=bool(steps), supported=True, led=led, steps=steps,
                     msg="; ".join(notes) if notes else "")


if __name__ == "__main__":
    main()
