#!/usr/bin/python
"""Set a board's preset, transmit power and transmitter over its shell."""

DOCUMENTATION = r"""
module: meshtastic_radio
short_description: Preset, transmit power and transmitter of a board
description:
  - Brings a board's stored LoRa preset, transmit power and transmitter switch to what is
    declared, over the plain shell through the board's serial broker. It is the first thing
    applied to a board after a flash, and it works on a keyless head.
  - The transmitter is switched off while the preset or the power is wrong, the power is set
    before the preset, and the transmitter goes back on last.
  - Reads the board, changes only what differs, then reads it again; success is the second
    read matching, never the text a command printed. Supports check mode and diff.
  - Needs firmware with the C(meshtastic state) command.
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
  lora:
    description: The declared radio config, as in a layout's C(meshtastic.config.lora).
      Only C(modem_preset), C(tx_power), C(tx_enabled) and C(profile) (firmware with radio profiles) are managed here; a key that is
      absent is left as it is. C(region) and C(use_preset) are accepted and ignored.
    type: dict
    required: true
  check_live:
    description: After a write, also require the radio to be on the stored preset. Turn it
      off for a board whose radio is elsewhere on purpose (a listener sweeping presets).
    type: bool
    default: true
  rf_rules:
    description: The RF rules the layout resolved to (C(bench_rf_rules)). The block is checked
      against them again here, before anything is read or written, so a value that reached the
      play past the layout's own check is refused too. Absent, the built-in rules.
    type: dict
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  deadline:
    description: Seconds allowed for each shell command.
    type: float
    default: 10.0
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Radio config first
  rxrbench.mesh.meshtastic_radio:
    board: "{{ bench_board }}"
    lora: "{{ meshtastic.config.lora }}"
"""

RETURN = r"""
commands:
  description: The shell commands that were (or, in check mode, would be) run, in order.
  returned: always
  type: list
  elements: str
lora:
  description: The board's radio config after the run (in check mode, as they are).
  returned: success
  type: dict
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import meshrf, radio, state


def read_facts(board, deadline):
    facts = state.read(board, deadline)
    if facts is None:
        # Older firmware: the same config, read from its prose. (A plan made before a
        # roll meets it; the roll itself flashes first.)
        facts = state.legacy_facts({c: board.exec(c, deadline=deadline)
                                    for c in state.LEGACY_COMMANDS})
    return facts


def read_lora(board, deadline):
    return read_facts(board, deadline)["lora"]


def shown(lora):
    return {f: lora.get(f) for f in radio.FIELDS}


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "board": {"type": "str", "required": True},
            "lora": {"type": "dict", "required": True},
            "check_live": {"type": "bool", "default": True},
            "rf_rules": {"type": "dict"},
            "socket": {"type": "path"},
            "deadline": {"type": "float", "default": 10.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            facts = read_facts(b, p["deadline"])
            before = facts["lora"]
            refused = meshrf.check_write("meshtastic", p["board"], p["lora"], p["rf_rules"],
                                          tx_now=before.get("tx_enabled"))
            if refused:
                module.fail_json(msg="the RF rules refuse this write: " + "; ".join(refused))
            cmds, delta = radio.plan(p["lora"], before)
            result = {"changed": bool(cmds), "commands": cmds, "lora": before}
            if cmds:
                result["diff"] = {"before": shown(before),
                                  "after": {**shown(before), **{f: v[1] for f, v in delta.items()}}}
            if cmds and facts.get("managed"):
                # Its shell refuses every write, as its phone API does: say so before sending.
                module.fail_json(msg=f"{p['board']}: the node is managed (security.is_managed): "
                                     "it takes configuration only from a remote admin, so the "
                                     f"bench cannot change its {', '.join(sorted(delta))}. "
                                     "Change it with a node whose key is among its admin keys, "
                                     "or erase its config region", **result)
            if not cmds or module.check_mode:
                module.exit_json(**result)
            gate.check(module)   # the layout's go-ahead, before the first write
            for cmd in cmds:
                b.exec(cmd, deadline=p["deadline"])
            after = read_lora(b, p["deadline"])
            left = radio.diff(p["lora"], after)
            if left:
                module.fail_json(msg="the board did not take: " + ", ".join(
                    f"{f} is {v[0]!r}, wanted {v[1]!r}" for f, v in sorted(left.items())),
                    commands=cmds, lora=after)
            if p["check_live"] and after.get("live_preset") != after.get("modem_preset"):
                module.fail_json(msg=f"the preset is stored as {after.get('modem_preset')} but "
                                     f"the radio is on {after.get('live_preset')}",
                                 commands=cmds, lora=after)
            result["lora"] = after
            module.exit_json(**result)
    except (broker_client.BrokerError, state.StateError, radio.RadioError) as e:
        module.fail_json(msg=str(e))


if __name__ == "__main__":
    main()
