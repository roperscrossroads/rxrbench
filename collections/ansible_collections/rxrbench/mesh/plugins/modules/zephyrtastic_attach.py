#!/usr/bin/python
"""Store, on a brain, the preset it wants of each of its heads."""

DOCUMENTATION = r"""
module: zephyrtastic_attach
short_description: The preset a brain wants of each of its heads
description:
  - A brain keeps, per head, the preset it wants that head on, and re-sends it whenever the
    head reports another (so a head that is reflashed or reboots is put back unprompted).
    This module brings that stored wish to what the layout declares, over the brain's shell
    (C(meshtastic attach preset)).
  - The layout names heads by board. Each head's node id is read from the head's own state
    line, through its own broker, and matched to the brain's table; a declared head that is
    not attached is an error. An attached head the layout does not name is left alone.
  - Reads the brain, changes only what differs, then reads it again until every declared head
    is wanted on its preset and, where its link is up, is on it. Supports check mode and diff.
  - Needs firmware whose state line carries C(brain.heads[].w) (0.4.39 on).
options:
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode), a read and a board that already matches need none.
    type: bool
    default: false
  board:
    description: The brain's name.
    type: str
    required: true
  heads:
    description: "The declared heads, C({board: PRESET}), as in a layout's C(zephyrtastic.brain.heads)."
    type: dict
    required: true
  head_sockets:
    description: "The broker socket of each head, C({board: path}), where it is not the default."
    type: dict
    default: {}
  socket:
    description: The brain's broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  deadline:
    description: Seconds allowed for each shell command.
    type: float
    default: 10.0
  attach_wait:
    description: Seconds the declared heads have to attach (dial the brain again) before one
      that has not is an error; a brain that was just flashed has none at first.
    type: float
    default: 90.0
  settle:
    description: Seconds to wait, after a change, for every head to be on its preset.
    type: float
    default: 45.0
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: What the brain wants of its heads
  rxrbench.mesh.zephyrtastic_attach:
    board: brain1
    heads: {head1: MEDIUM_FAST, head2: SHORT_FAST}
"""

RETURN = r"""
commands:
  description: The shell commands that were (or, in check mode, would be) run, in order.
  returned: always
  type: list
  elements: str
heads:
  description: The brain's table after the run (in check mode, as it is).
  returned: success
  type: list
  elements: dict
node_ids:
  description: The node id each declared head reported, by board.
  returned: success
  type: dict
"""

import time

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import attach, state


def read_state(board, deadline):
    facts = state.state_facts(board.exec(state.STATE_COMMAND, deadline=deadline))
    if facts is None:
        raise state.StateError(f"{board.board}: no state line (firmware too old)")
    return facts


def read_brain(board, deadline):
    brain = read_state(board, deadline).get("brain")
    if brain is None:
        raise state.StateError(f"{board.board} is not a brain (its state line has no heads)")
    return brain


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "board": {"type": "str", "required": True},
            "heads": {"type": "dict", "required": True},
            "head_sockets": {"type": "dict", "default": {}},
            "socket": {"type": "path"},
            "deadline": {"type": "float", "default": 10.0},
            "settle": {"type": "float", "default": 45.0},
            "attach_wait": {"type": "float", "default": 90.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    declared = p["heads"]
    try:
        attach.check_heads(declared)
        ids = {}
        for head in sorted(declared):
            with broker_client.Board(head, path=p["head_sockets"].get(head) or None) as h:
                ids[head] = read_state(h, p["deadline"])["node_id"]
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            # A brain that has just rebooted (its flash a moment ago) has no heads until each
            # head dials it again over BLE: give them `attach_wait` seconds to come back.
            end = time.monotonic() + p["attach_wait"]
            while True:
                brain = read_brain(b, p["deadline"])
                attached = {attach.node_key(h["node_id"]) for h in brain.get("heads") or []}
                if {attach.node_key(i) for i in ids.values()} <= attached \
                        or time.monotonic() >= end:
                    break
                time.sleep(2.0)
            cmds, delta = attach.plan_heads(declared, ids, brain)
            result = {"changed": bool(cmds), "commands": cmds, "heads": brain["heads"],
                      "node_ids": ids}
            if cmds:
                result["diff"] = {
                    "before": {h: v[0] for h, v in delta.items()},
                    "after": {h: v[1] for h, v in delta.items()},
                }
            if not cmds or module.check_mode:
                module.exit_json(**result)
            gate.check(module)   # the layout's go-ahead, before the first write
            for cmd in cmds:
                b.exec(cmd, deadline=p["deadline"])
            # Stored at once; the head moves when the brain's ask reaches it and it reports
            # back. Wait for both, but never for a head whose link is down.
            end = time.monotonic() + p["settle"]
            while True:
                brain = read_brain(b, p["deadline"])
                left = attach.heads_left(declared, ids, brain)
                if not left or time.monotonic() >= end:
                    break
                time.sleep(1.0)
            result["heads"] = brain["heads"]
            if left:
                module.fail_json(msg="not as declared: " + "; ".join(
                    f"{h}: {why}" for h, why in sorted(left.items())), **result)
            module.exit_json(**result)
    except (broker_client.BrokerError, state.StateError, attach.AttachError) as e:
        module.fail_json(msg=str(e))


if __name__ == "__main__":
    main()
