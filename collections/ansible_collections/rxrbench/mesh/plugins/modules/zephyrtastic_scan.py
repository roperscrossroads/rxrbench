#!/usr/bin/python
"""Store the presets a listener sweeps."""

DOCUMENTATION = r"""
module: zephyrtastic_scan
short_description: The presets a listener sweeps
description:
  - A listener (a scanner image) sweeps a list of presets with its transmitter gated shut.
    The list is stored on the board (C(meshtastic scan presets)) and beats the build's own
    list at boot. This module brings the stored list to what the layout declares, order
    included (it is the order of the sweep).
  - Reads the board, changes only what differs, then reads it again. Supports check mode and
    diff. Setting a list clears the scanner's statistics; an unchanged list is not set again.
options:
  board:
    description: The listener's name.
    type: str
    required: true
  presets:
    description: The declared list, as in a layout's C(zephyrtastic.listener.presets).
    type: list
    elements: str
    required: true
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
- name: What the listener sweeps
  rxrbench.mesh.zephyrtastic_scan:
    board: listener1
    presets: [MEDIUM_FAST]
"""

RETURN = r"""
commands:
  description: The shell commands that were (or, in check mode, would be) run.
  returned: always
  type: list
  elements: str
presets:
  description: The list the board sweeps after the run (in check mode, as it is).
  returned: success
  type: list
  elements: str
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import attach, state


def read_scanner(board, deadline):
    facts = state.state_facts(board.exec(state.STATE_COMMAND, deadline=deadline))
    if facts is None:
        raise state.StateError(f"{board.board}: no state line (firmware too old)")
    if "scanner" not in facts:
        raise state.StateError(f"{board.board} is not a listener (its state line has no scan)")
    return facts["scanner"]


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "presets": {"type": "list", "elements": "str", "required": True},
            "socket": {"type": "path"},
            "deadline": {"type": "float", "default": 10.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            before = read_scanner(b, p["deadline"])
            cmds, delta = attach.plan_scan(p["presets"], before)
            result = {"changed": bool(cmds), "commands": cmds, "presets": before["presets"]}
            if cmds:
                result["diff"] = {"before": {"presets": delta[0]}, "after": {"presets": delta[1]}}
            if not cmds or module.check_mode:
                module.exit_json(**result)
            for cmd in cmds:
                b.exec(cmd, deadline=p["deadline"])
            after = read_scanner(b, p["deadline"])
            result["presets"] = after["presets"]
            if after["presets"] != list(p["presets"]):
                module.fail_json(msg=f"the board did not take: it sweeps {after['presets']}, "
                                     f"declared {p['presets']}", **result)
            module.exit_json(**result)
    except (broker_client.BrokerError, state.StateError, attach.AttachError) as e:
        module.fail_json(msg=str(e))


if __name__ == "__main__":
    main()
