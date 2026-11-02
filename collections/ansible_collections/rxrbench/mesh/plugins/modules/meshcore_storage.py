#!/usr/bin/python
"""Bring a MeshCore board's storage to its declared epoch: a wipe as declared state."""

DOCUMENTATION = r"""
module: meshcore_storage
short_description: A zephcore node's storage epoch (wipe as declared state)
description:
  - "A zephcore node keeps a storage epoch, an integer its wipe writes last. A layout declares
    one; when the node's differs, this wipes it (`zephcore wipe <epoch> keep|new`), waits for
    it to reboot and checks the epoch, and the identity: kept or new, as declared. The same
    epoch changes nothing, so apply stays idempotent; bumping it wipes the group once."
  - "A wipe leaves the node receive-only with default config: the radio and config tasks run
    after this one."
  - Supports check mode (says whether it would wipe). A backup before the wipe is the
    playbook's (region_backup with the image's map), where a board has one.
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
  family:
    description: The board's firmware family (zephcore; a family with a state command).
    type: str
    default: zephcore
  epoch:
    description: The declared epoch, an integer >= 1.
    type: int
    required: true
  keep_identity:
    description: Keep the node's key (its advert hash, its place in others' contacts).
    type: bool
    default: true
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  settle:
    description: Seconds the wipe and reboot may take.
    type: float
    default: 60.0
  families:
    description: The device families as data, the inventory's C(bench_families). It says how
      a board's identity is read and what an image of a family must match.
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Storage epoch
  rxrbench.mesh.meshcore_storage:
    board: "{{ bench_board }}"
    epoch: "{{ storage.zephcore.epoch }}"
    keep_identity: "{{ storage.zephcore.keep_identity | default(true) }}"
"""

RETURN = r"""
wiped:
  description: The wipe command run, or null.
  returned: always
  type: str
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import meshcore, state


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "family": {"type": "str", "default": "zephcore"},
            "epoch": {"type": "int", "required": True},
            "keep_identity": {"type": "bool", "default": True},
            "socket": {"type": "path"},
            "settle": {"type": "float", "default": 60.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    familyspec.load(p["families"])
    command = familyspec.state_command(familyspec.of(p["family"]))
    if not command:
        module.fail_json(msg=f"{p['family']} firmware has no state line to read an epoch from")
    try:
        with broker_client.Board(p["board"], path=p["socket"], timeout=p["settle"] + 60) as b:
            before = meshcore.read(b, p["family"], command)
            step = meshcore.storage_plan({"epoch": p["epoch"], "keep_identity": p["keep_identity"]},
                                         before)
            diff = {"before": {"epoch": (before.get("storage") or {}).get("epoch")},
                    "after": {"epoch": p["epoch"]}}
            if step is None or module.check_mode:
                module.exit_json(changed=step is not None, wiped=step, diff=diff if step else {})
            gate.check(module)   # the layout's go-ahead, before the first write
            after = meshcore.wipe(b, step, p["family"], command, p["settle"])
    except (broker_client.BrokerError, state.StateError, meshcore.MeshCoreError) as e:
        module.fail_json(msg=str(e))
    if (after.get("storage") or {}).get("epoch") != p["epoch"]:
        module.fail_json(msg=f"{p['board']}: after the wipe its epoch is "
                             f"{(after.get('storage') or {}).get('epoch')}, not {p['epoch']}")
    same_key = after.get("pubkey_fp") == before.get("pubkey_fp")
    if same_key != p["keep_identity"]:
        module.fail_json(msg=f"{p['board']}: the wipe {'replaced' if not same_key else 'kept'} "
                             f"the identity, declared {'keep' if p['keep_identity'] else 'new'}")
    module.exit_json(changed=True, wiped=step, diff=diff)


if __name__ == "__main__":
    main()
