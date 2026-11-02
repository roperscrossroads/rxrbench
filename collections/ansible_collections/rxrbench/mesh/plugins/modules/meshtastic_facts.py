#!/usr/bin/python
"""Read a board's actual state through its serial broker."""

DOCUMENTATION = r"""
module: meshtastic_facts
short_description: Read a board's actual state
description:
  - Reads a board's state through the serial broker on its bench host and returns it in the
    stock Meshtastic vocabulary, so it compares directly with a layout's declared state.
  - Never changes anything, and behaves the same in check mode.
  - Firmware without the machine-readable C(meshtastic state) command is read from a few stable
    lines of its prose output; the result then says C(state_supported=false), and anything not
    in it is unknown, not absent.
options:
  families:
    description: The firmware family table (the play's C(bench_families)). With it, a board
      that does not answer as its declared family is asked as the others, and the failure says
      what it runs instead.
    type: dict
  board:
    description: The board's name.
    type: str
    required: true
  family:
    description: The family the layout declares (one of this vocabulary).
    type: str
    default: zephyrtastic
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
- name: Read the board
  rxrbench.mesh.meshtastic_facts:
    board: "{{ bench_board }}"
  register: actual
"""

RETURN = r"""
mesh:
  description: The board's state.
  returned: success
  type: dict
  sample:
    state_supported: false
    build: 9a4d0d25
    image: 0.4.36+0
    class: 4
    node_id: "0x0a0b0c0d"
    lora: {modem_preset: SHORT_TURBO, live_preset: SHORT_TURBO, region: US, tx_power: 2, tx_enabled: true}
    device: {role: CLIENT, rebroadcast_mode: ALL}
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, familyspec
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from ansible_collections.rxrbench.mesh.plugins.module_utils import state


def main():
    module = AnsibleModule(
        argument_spec={
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "family": {"type": "str", "default": "zephyrtastic"},
            "socket": {"type": "path"},
            "deadline": {"type": "float", "default": 10.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    if p["families"]:
        familyspec.load(p["families"])
    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            try:
                facts = state.read(b, p["deadline"])
                if facts is None:  # firmware older than the state command
                    outputs = {c: b.exec(c, deadline=p["deadline"]) for c in state.LEGACY_COMMANDS}
                    facts = state.legacy_facts(outputs)
            except (broker_client.BrokerError, state.StateError) as e:
                instead = common.runs_instead(b, p["family"], p["deadline"]) if p["families"] else None
                raise state.StateError(f"{e}: {instead}" if instead else str(e)) from e
    except (broker_client.BrokerError, state.StateError) as e:
        module.fail_json(msg=str(e))
    module.exit_json(changed=False, mesh=facts)


if __name__ == "__main__":
    main()
