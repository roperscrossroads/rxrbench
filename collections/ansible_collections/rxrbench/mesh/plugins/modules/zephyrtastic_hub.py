#!/usr/bin/python
"""Store, on a hub, the preset it wants of each of its workers."""

DOCUMENTATION = r"""
module: zephyrtastic_hub
short_description: The preset a hub wants of each of its workers
description:
  - A hub keeps, per worker, the preset it wants that worker on, and re-sends it whenever the
    worker reports another (so a worker that is reflashed or reboots is put back unprompted).
    This module brings that stored wish to what the layout declares, over the hub's shell
    (C(meshtastic hub preset); on firmware from before the rename, C(meshtastic attach preset)).
  - The layout names workers by board. Each worker's node id is read from the worker's own
    state line, through its own broker, and matched to the hub's table; a declared worker that
    is not linked is an error. A linked worker the layout does not name is left alone.
  - Reads the hub, changes only what differs, then reads it again until every declared worker
    is wanted on its preset and, where its link is up, is on it. Supports check mode and diff.
  - Needs firmware whose state line carries the hub's workers with their wanted preset
    (C(hub.workers[].w), or C(brain.heads[].w) before the rename; 0.4.39 on).
options:
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode), a read and a board that already matches need none.
    type: bool
    default: false
  board:
    description: The hub's name.
    type: str
    required: true
  workers:
    description: "The declared workers, C({board: PRESET}), as in a layout's C(zephyrtastic.hub.workers)."
    type: dict
    required: true
  worker_sockets:
    description: "The broker socket of each worker, C({board: path}), where it is not the default."
    type: dict
    default: {}
  socket:
    description: The hub's broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  deadline:
    description: Seconds allowed for each shell command.
    type: float
    default: 10.0
  link_wait:
    description: Seconds the declared workers have to link (dial the hub again) before one
      that has not is an error; a hub that was just flashed has none at first.
    type: float
    default: 90.0
  settle:
    description: Seconds to wait, after a change, for every worker to be on its preset.
    type: float
    default: 45.0
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: What the hub wants of its workers
  rxrbench.mesh.zephyrtastic_hub:
    board: hub1
    workers: {worker1: MEDIUM_FAST, worker2: SHORT_FAST}
"""

RETURN = r"""
commands:
  description: The shell commands that were (or, in check mode, would be) run, in order, in
    their new spelling (a board on older firmware is sent the old one).
  returned: always
  type: list
  elements: str
workers:
  description: The hub's table after the run (in check mode, as it is).
  returned: success
  type: list
  elements: dict
node_ids:
  description: The node id each declared worker reported, by board.
  returned: success
  type: dict
"""

import time

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import hub, state


def read_state(board, deadline):
    facts = state.state_facts(board.exec(state.STATE_COMMAND, deadline=deadline))
    if facts is None:
        raise state.StateError(f"{board.board}: no state line (firmware too old)")
    return facts


def read_hub(board, deadline):
    facts = read_state(board, deadline).get("hub")
    if facts is None:
        raise state.StateError(f"{board.board} is not a hub (its state line has no workers)")
    return facts


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "board": {"type": "str", "required": True},
            "workers": {"type": "dict", "required": True},
            "worker_sockets": {"type": "dict", "default": {}},
            "socket": {"type": "path"},
            "deadline": {"type": "float", "default": 10.0},
            "settle": {"type": "float", "default": 45.0},
            "link_wait": {"type": "float", "default": 90.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    declared = p["workers"]
    try:
        hub.check_workers(declared)
        ids = {}
        for worker in sorted(declared):
            with broker_client.Board(worker, path=p["worker_sockets"].get(worker) or None) as w:
                ids[worker] = read_state(w, p["deadline"])["node_id"]
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            # A hub that has just rebooted (its flash a moment ago) has no workers until each
            # worker dials it again over BLE: give them `link_wait` seconds to come back.
            end = time.monotonic() + p["link_wait"]
            while True:
                facts = read_hub(b, p["deadline"])
                linked = {hub.node_key(w["node_id"]) for w in facts.get("workers") or []}
                if {hub.node_key(i) for i in ids.values()} <= linked \
                        or time.monotonic() >= end:
                    break
                time.sleep(2.0)
            cmds, delta = hub.plan_workers(declared, ids, facts)
            result = {"changed": bool(cmds), "commands": cmds, "workers": facts["workers"],
                      "node_ids": ids}
            if cmds:
                result["diff"] = {
                    "before": {w: v[0] for w, v in delta.items()},
                    "after": {w: v[1] for w, v in delta.items()},
                }
            if not cmds or module.check_mode:
                module.exit_json(**result)
            gate.check(module)   # the layout's go-ahead, before the first write
            for cmd in cmds:
                hub.run(b, cmd, p["deadline"])
            # Stored at once; the worker moves when the hub's ask reaches it and it reports
            # back. Wait for both, but never for a worker whose link is down.
            end = time.monotonic() + p["settle"]
            while True:
                facts = read_hub(b, p["deadline"])
                left = hub.workers_left(declared, ids, facts)
                if not left or time.monotonic() >= end:
                    break
                time.sleep(1.0)
            result["workers"] = facts["workers"]
            if left:
                module.fail_json(msg="not as declared: " + "; ".join(
                    f"{w}: {why}" for w, why in sorted(left.items())), **result)
            module.exit_json(**result)
    except (broker_client.BrokerError, state.StateError, hub.HubError) as e:
        module.fail_json(msg=str(e))


if __name__ == "__main__":
    main()
