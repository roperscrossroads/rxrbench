#!/usr/bin/python
"""Bring a MeshCore board's radio to its declared state."""

DOCUMENTATION = r"""
module: meshcore_radio
short_description: Radio config of a MeshCore board (zephcore, stock MeshCore)
description:
  - "Reads the board's radio (frequency, bandwidth, SF, CR, power, whether it may transmit),
    changes only what differs, in a safe order: the transmitter off before power or parameters
    change; power; the parameters, which MeshCore applies at the next reboot, so the board is
    rebooted and waited for; the transmitter on again only at the end, and only when declared.
    Then reads the board again and fails unless it is as declared."
  - A zephcore node starts receive-only (fresh, or after a wipe), so C(tx_enabled) true is what
    lets it transmit at all.
  - "Stock MeshCore has no transmitter switch: it transmits whenever it is powered. There the
    transmitter steps are left out, the read-back does not compare C(tx_enabled), and a declared
    C(tx_enabled) is for the RF rules only."
  - Supports check mode (the plan) and diff.
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
  radio:
    description: The declared radio, C({freq_mhz, bw_khz, sf, cr, tx_dbm, tx_enabled}) or a named
      C(preset) (usa) with power and transmitter; keys left out are left as they are.
    type: dict
    required: true
  rf_rules:
    description: The RF rules the layout resolved to (C(bench_rf_rules)). The block is checked
      against them again here, before anything is read or written, so a value that reached the
      play past the layout's own check is refused too. Absent, the built-in rules.
    type: dict
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  settle:
    description: Seconds a reboot may take before the board answers again.
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
- name: Radio config
  rxrbench.mesh.meshcore_radio:
    board: "{{ bench_board }}"
    radio: "{{ meshcore.radio }}"
"""

RETURN = r"""
steps:
  description: The console commands sent, in order, with the board's answers.
  returned: always
  type: list
  elements: str
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import meshcore, meshrf, state


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "family": {"type": "str", "default": "zephcore"},
            "radio": {"type": "dict", "required": True},
            "rf_rules": {"type": "dict"},
            "socket": {"type": "path"},
            "settle": {"type": "float", "default": 60.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    familyspec.load(p["families"])
    try:
        p["radio"] = meshcore.declared_radio(p["radio"])   # a named preset, expanded
    except meshcore.MeshCoreError as e:
        module.fail_json(msg=str(e))
    command = familyspec.state_command(familyspec.of(p["family"]))
    try:
        with broker_client.Board(p["board"], path=p["socket"], timeout=p["settle"] + 60) as b:
            before = meshcore.read(b, p["family"], command)
            refused = meshrf.check_write("meshcore", p["board"], p["radio"], p["rf_rules"],
                                          tx_now=bool((before.get("radio") or {}).get("tx_enabled")))
            if refused:
                module.fail_json(msg="the RF rules refuse this write: " + "; ".join(refused))
            plan = meshcore.radio_plan(p["radio"], before)
            diff = {"before": {k: before["radio"].get(k) for k in p["radio"]},
                    "after": dict(p["radio"])}
            if not plan or module.check_mode:
                module.exit_json(changed=bool(plan), steps=plan, diff=diff if plan else {})
            gate.check(module)   # the layout's go-ahead, before the first write
            steps = meshcore.run_steps(b, plan, p["family"], command, p["settle"])
            after = meshcore.read(b, p["family"], command)
    except (broker_client.BrokerError, state.StateError, meshcore.MeshCoreError) as e:
        module.fail_json(msg=str(e))
    left = meshcore.radio_plan(p["radio"], after)
    if left:
        module.fail_json(msg=f"{p['board']}: after the writes the radio still differs: {left}",
                         steps=steps)
    module.exit_json(changed=True, steps=steps, diff=diff)


if __name__ == "__main__":
    main()
