#!/usr/bin/python
"""Forget the roles a node remembers, when the layout declares it has none."""
DOCUMENTATION = r"""
module: zephyrtastic_intents
short_description: The roles a zephyrtastic node remembers across reboots
description:
  - A node keeps its role intents in its config, in Zephyr's settings store (the BLE peer it dials, the hub it reports
    to, the workers it wants, the rx-only worker's receiving half, the courier's arm, the relay's direction,
    a pinned preset list, a cluster document) and restores them at boot. None keys off a
    declared setting, so a board moved from a hub setup layout to a plain one kept dialling its old
    peers. A plain profile declares C(zephyrtastic.intents) C(none); this module reads what
    the node remembers (C(meshtastic intents)), and when anything is set, forgets it all
    (C(meshtastic intents clear)), waits for the reboot that follows, and reads again.
  - Identity, radio, channels, owner and device config are untouched. Supports check mode
    and diff.
options:
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode), a read and a board that already matches need none.
    type: bool
    default: false
  board:
    description: The node's name.
    type: str
    required: true
  intents:
    description: The declaration; C(none) is the only one (a role is declared by its own setting).
    type: str
    required: true
    choices: [none]
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  deadline:
    description: Seconds allowed for each shell command.
    type: float
    default: 10.0
  settle:
    description: Seconds the node has to come back after the clear's reboot.
    type: float
    default: 90.0
author:
  - Adam Roper (@roperscrossroads)
"""
EXAMPLES = r"""
- name: A plain node remembers no role
  rxrbench.mesh.zephyrtastic_intents:
    board: node1
    intents: none
"""
RETURN = r"""
commands:
  description: The shell commands that were (or, in check mode, would be) run.
  returned: always
  type: list
  elements: str
set:
  description: The intents the node has after the run (in check mode, as it is), name to detail.
  returned: success
  type: dict
"""
import time

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import intents, state


def read(b, deadline):
    return intents.parse(b.exec(intents.INTENT_COMMAND, deadline=deadline))


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "board": {"type": "str", "required": True},
            "intents": {"type": "str", "required": True, "choices": ["none"]},
            "socket": {"type": "path"},
            "deadline": {"type": "float", "default": 10.0},
            "settle": {"type": "float", "default": 90.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            try:
                before = read(b, p["deadline"])
            except intents.IntentsError as e:
                # Firmware without the verb. In a plan the flash above may be bringing one that
                # has it, and the roles are read once it is written: say so, do not fail the
                # plan. In an apply the board should have it by now.
                if module.check_mode:
                    module.exit_json(changed=True, commands=[intents.CLEAR_COMMAND], set={},
                                     msg=f"{p['board']}: {e}; the roles it remembers are read "
                                         "once it runs a firmware with `meshtastic intents`")
                raise
            cmds, was, want = intents.plan(p["intents"], before)
            result = {"changed": bool(cmds), "commands": cmds, "set": was}
            if cmds:
                result["diff"] = {"before": {"intents": was}, "after": {"intents": want}}
            if not cmds or module.check_mode:
                module.exit_json(**result)
            gate.check(module)   # the layout's go-ahead, before the first write
            # The clear answers, then the node reboots: the answer may be the last thing heard
            # for a while, and a command sent into the reboot fails. Wait for the listing again.
            try:
                b.exec(intents.CLEAR_COMMAND, deadline=p["deadline"])
            except broker_client.BrokerError:
                pass
            end = time.monotonic() + p["settle"]
            after, why = None, "the node did not answer again"
            time.sleep(2.0)
            while time.monotonic() < end:
                try:
                    after = read(b, p["deadline"])
                    break
                except (broker_client.BrokerError, state.StateError, intents.IntentsError) as e:
                    why = str(e)
                time.sleep(1.0)
            if after is None:
                module.fail_json(msg=f"after `intents clear`: {why}", **result)
            result["set"] = after["set"]
            if after["set"]:
                module.fail_json(msg=f"{p['board']} still remembers {sorted(after['set'])} after "
                                     "the clear and the reboot", **result)
            module.exit_json(**result)
    except (broker_client.BrokerError, state.StateError, intents.IntentsError) as e:
        module.fail_json(msg=str(e))


if __name__ == "__main__":
    main()
