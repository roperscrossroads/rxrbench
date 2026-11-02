#!/usr/bin/python
"""Read a MeshCore-vocabulary node's identity key, or give it back one."""

DOCUMENTATION = r"""
module: meshcore_identity
short_description: A MeshCore node's identity key, for escrow, and put back from escrow
description:
  - "A MeshCore node is known by its public key; a wipe with C(new), a reflash that loses the
    file system, or another family's image gives it a new one. This module is the key level of
    the backups for the MeshCore vocabulary, as C(meshtastic_identity) is for Meshtastic."
  - "C(action=read) exports the private key (zephcore: C(zephcore key export confirm), one
    C(~K) line; stock MeshCore: C(get prv.key), its hex answer; the broker logs neither) and
    returns it base64, with the public key's fingerprint as C(node_id). Register it with
    C(no_log: true)."
  - "C(action=ensure) compares the node's key with C(private_key) and, if they differ, writes
    it (C(set prv.key), never echoed), reboots the node, and requires it to come back with
    C(node_id) and the escrowed key. Check mode reports whether it would write."
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
  action:
    description: C(read) the key, or C(ensure) the node holds C(private_key).
    type: str
    choices: [read, ensure]
    default: read
  private_key:
    description: (ensure) The escrowed private key, base64 of 64 bytes.
    type: str
  node_id:
    description: (ensure) The public key fingerprint the escrowed key gives (C(pubkey_fp)).
    type: str
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  settle:
    description: Seconds the node has to come back after the reboot.
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
- name: Read the key for escrow
  rxrbench.mesh.meshcore_identity:
    board: "{{ bench_board }}"
  register: ident
  no_log: true
"""

RETURN = r"""
node_id:
  description: The node's public key fingerprint (after the write, in ensure mode).
  returned: always
  type: str
private_key:
  description: (read) The private key, base64. A secret.
  returned: action=read
  type: str
"""

import base64
import binascii

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
            "action": {"type": "str", "choices": ["read", "ensure"], "default": "read"},
            "private_key": {"type": "str", "no_log": True},
            "node_id": {"type": "str"},
            "socket": {"type": "path"},
            "settle": {"type": "float", "default": 60.0},
        },
        required_if=[("action", "ensure", ("private_key", "node_id"))],
        supports_check_mode=True,
    )
    p = module.params
    familyspec.load(p["families"])
    command = familyspec.state_command(familyspec.of(p["family"]))
    try:
        with broker_client.Board(p["board"], path=p["socket"], timeout=p["settle"] + 60) as b:
            facts = meshcore.read(b, p["family"], command)
            key = meshcore.key_export(b, command)
            if p["action"] == "read":
                module.exit_json(changed=False, node_id=facts["pubkey_fp"],
                                 public_key_fp=facts["pubkey_fp"],
                                 private_key=base64.b64encode(key).decode())
            want = base64.b64decode(p["private_key"], validate=True)
            if len(want) != meshcore.KEY_LEN:
                module.fail_json(msg=f"the escrowed key is not {meshcore.KEY_LEN} bytes")
            want_id = p["node_id"].lower()
            if want == key:
                if facts["pubkey_fp"] != want_id:
                    module.fail_json(msg=f"{p['board']} holds the escrowed key but reports "
                                         f"{facts['pubkey_fp']}, the escrow says {want_id}")
                module.exit_json(changed=False, node_id=want_id)
            result = {"changed": True, "node_id_before": facts["pubkey_fp"], "node_id": want_id}
            if module.check_mode:
                module.exit_json(msg=f"would give {p['board']} its escrowed key: "
                                     f"{facts['pubkey_fp']} -> {want_id}", **result)
            gate.check(module)   # the layout's go-ahead, before the first write
            after = meshcore.key_import(b, p["family"], want, command, p["settle"])
            if after["pubkey_fp"] != want_id or meshcore.key_export(b, command) != want:
                module.fail_json(msg=f"{p['board']} came back as {after['pubkey_fp']}, not with "
                                     f"the escrowed key ({want_id})", **result)
    except (broker_client.BrokerError, state.StateError, meshcore.MeshCoreError, ValueError,
            binascii.Error) as e:
        module.fail_json(msg=str(e))
    module.exit_json(**result)


if __name__ == "__main__":
    main()
