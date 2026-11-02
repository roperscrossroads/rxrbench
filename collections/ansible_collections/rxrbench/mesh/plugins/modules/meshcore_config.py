#!/usr/bin/python
"""Set a MeshCore board's name, advert intervals, prefs and admin password through its CLI."""

DOCUMENTATION = r"""
module: meshcore_config
short_description: Name, advert intervals, prefs and admin password of a MeshCore board
description:
  - "Reads the board (its state line, and `get advert.interval` / `get flood.advert.interval`
    where those are declared), writes only what differs through MeshCore's CLI, and reads it
    again: the result must be as declared."
  - "The admin password is compared by its fingerprint (C(pw_fp) in the state line) and written
    with `password <pw>`, which the firmware neither echoes nor answers with the value: it never
    reaches the console log, this module's output or its arguments in a log (no_log). Stock
    MeshCore prints no fingerprint: there the password is written together with any other
    change, or when C(restored) says the node was just brought back, and otherwise left alone."
  - "C(prefs) are other CLI prefs ({key: value}: lat, lon, txdelay, owner.info, ...), read
    with `get <key>` and written with `set <key> <value>` when they differ (numbers compared
    as numbers, booleans as on/off)."
  - Supports check mode (the plan) and diff. The radio is meshcore_radio's.
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
    description: The board's firmware family (vocabulary meshcore).
    type: str
    default: zephcore
  name:
    description: "The node's name, 1 to 31 bytes, spaces allowed; MeshCore refuses brackets,
      a backslash, colon, comma, question mark or asterisk in it."
    type: str
  prefs:
    description: "Other CLI prefs, C({key: value})."
    type: dict
  restored:
    description: "The node was just brought back (its key given back, its radio or storage
      rewritten): a password that cannot be compared is written."
    type: bool
    default: false
  advert:
    description: C({local_interval_min, flood_interval_h}); 0 turns one off.
    type: dict
  admin_password:
    description: The repeater's admin password, 9 to 15 characters, from the site's secrets.
    type: str
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  families:
    description: The device families as data, the inventory's C(bench_families). It says how
      a board's identity is read and what an image of a family must match.
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Name, adverts, password
  rxrbench.mesh.meshcore_config:
    board: "{{ bench_board }}"
    name: "{{ meshcore.name | default(omit) }}"
    advert: "{{ meshcore.advert | default(omit) }}"
    admin_password: "{{ meshcore.admin_password | default(omit) }}"
"""

RETURN = r"""
steps:
  description: The console commands sent (a password shown as <secret>), with the answers.
  returned: always
  type: list
  elements: str
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import meshcore, state


def gets_for(board, desired):
    adv = desired.get("advert") or {}
    out = {}
    for key, field in (("advert.interval", "local_interval_min"),
                       ("flood.advert.interval", "flood_interval_h")):
        if adv.get(field) is not None:
            out[key] = meshcore.value(board.exec(f"get {key}", dialect="meshcore"))
    for key in desired.get("prefs") or {}:
        out[key] = meshcore.value(board.exec(f"get {key}", dialect="meshcore"))
    return out


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "family": {"type": "str", "default": "zephcore"},
            "name": {"type": "str"},
            "advert": {"type": "dict"},
            "prefs": {"type": "dict"},
            "restored": {"type": "bool", "default": False},
            "admin_password": {"type": "str", "no_log": True},
            "socket": {"type": "path"},
        },
        supports_check_mode=True,
    )
    p = module.params
    familyspec.load(p["families"])
    desired = {k: p[k] for k in ("name", "advert", "prefs", "admin_password") if p[k] is not None}
    problems = meshcore.check(p["board"], desired)
    if problems:
        module.fail_json(msg="; ".join(problems))
    command = familyspec.state_command(familyspec.of(p["family"]))
    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            before = meshcore.read(b, p["family"], command)
            plan = meshcore.config_plan(desired, before, gets_for(b, desired), p["restored"])
            shown = [s[1] for s in plan]
            if not plan or module.check_mode:
                module.exit_json(changed=bool(plan), steps=shown)
            gate.check(module)   # the layout's go-ahead, before the first write
            steps = meshcore.run_steps(b, plan, p["family"], command)
            after = meshcore.read(b, p["family"], command)
            left = meshcore.config_plan(desired, after, gets_for(b, desired))
    except (broker_client.BrokerError, state.StateError, meshcore.MeshCoreError) as e:
        module.fail_json(msg=str(e))
    if left:
        module.fail_json(msg=f"{p['board']}: after the writes it still differs: "
                             f"{[s[1] for s in left]}", steps=steps)
    module.exit_json(changed=True, steps=steps)


if __name__ == "__main__":
    main()
