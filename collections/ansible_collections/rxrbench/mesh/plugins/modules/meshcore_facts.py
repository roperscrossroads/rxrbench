#!/usr/bin/python
"""Read a MeshCore-vocabulary board's actual state through its serial broker."""

DOCUMENTATION = r"""
module: meshcore_facts
short_description: Read a MeshCore board's actual state (zephcore, stock MeshCore)
description:
  - Reads a board that speaks MeshCore's CLI through the serial broker on its bench host, in
    the broker's MeshCore console dialect, and returns one dict that compares directly with a
    layout's C(meshcore) block.
  - "A zephcore node is read from its state line (C(zephcore state)); stock MeshCore, which has
    none, from single CLI answers (C(ver), C(get name), C(get radio), ...), and the result then
    says C(state_supported=false): anything not in it is unknown, not absent."
  - No secret is returned; the admin password only as a fingerprint (C(pw_fp)).
  - Never changes anything, and behaves the same in check mode.
options:
  board:
    description: The board's name.
    type: str
    required: true
  family:
    description: The board's firmware family, one with a MeshCore console.
    type: str
    default: zephcore
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  deadline:
    description: Seconds allowed for each console command.
    type: float
    default: 10.0
  families:
    description: The device families as data, the inventory's C(bench_families). It says how
      a board's identity is read and what an image of a family must match.
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Read the board
  rxrbench.mesh.meshcore_facts:
    board: "{{ bench_board }}"
    family: "{{ bench_family }}"
  register: actual
"""

RETURN = r"""
meshcore:
  description: The board's state.
  returned: success
  type: dict
  sample:
    state_supported: true
    family: zephcore
    version: 0.1.0
    build: 5962deb1
    role: repeater
    name: ridge-rpt-1
    pubkey_fp: c4d63bf2
    pw_fp: f5d6bda8
    radio: {freq_mhz: 910.525, bw_khz: 62.5, sf: 7, cr: 5, tx_dbm: 2, tx_enabled: true}
    storage: {fs: ok, epoch: 3, used: 20480, total: 122880}
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from ansible_collections.rxrbench.mesh.plugins.module_utils import meshcore, state


def main():
    module = AnsibleModule(
        argument_spec={
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "family": {"type": "str", "default": "zephcore"},
            "socket": {"type": "path"},
            "deadline": {"type": "float", "default": 10.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    familyspec.load(p["families"])
    spec = familyspec.of(p["family"])
    if spec["console"] != "meshcore":
        module.fail_json(msg=f"{p['board']}: family {p['family']!r} has no MeshCore console")
    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            try:
                facts = meshcore.read(b, p["family"], familyspec.state_command(spec),
                                      p["deadline"])
            except (broker_client.BrokerError, state.StateError, meshcore.MeshCoreError) as e:
                instead = common.runs_instead(b, p["family"], p["deadline"])
                raise meshcore.MeshCoreError(f"{e}: {instead}" if instead else str(e)) from e
    except (broker_client.BrokerError, state.StateError, meshcore.MeshCoreError) as e:
        module.fail_json(msg=str(e))
    module.exit_json(changed=False, meshcore=facts)


if __name__ == "__main__":
    main()
