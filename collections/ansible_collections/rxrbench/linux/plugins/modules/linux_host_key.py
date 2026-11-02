#!/usr/bin/python
"""Read a Linux device's ssh host key, or give it back one."""

DOCUMENTATION = r"""
module: linux_host_key
short_description: A Linux device's ssh host key, for escrow, and put back from escrow
description:
  - "A device makes its host key at first boot, and its peers know it by that key. A device
    that is re-imaged makes another and is a stranger to them. This module is the key level
    of the backups: the key pair alone, kept in the site's encrypted secrets."
  - "C(action=read) returns the key pair as one value and the fingerprint ssh shows for it.
    Register it with C(no_log: true): it holds the private key."
  - "C(action=ensure) compares the device's key pair with C(value) and, if they differ,
    replaces the two files. It then connects with the escrowed key pinned: a server that
    reads its key for each connection (OpenSSH) presents it at once, and that connection is
    the proof. A server that still presents the old key keeps it until it restarts; the
    module says which it saw and restarts nothing. Check mode reports whether it would write."
  - "After a restore the device presents its escrowed key: a pin of the key it had in
    between (C(known_hosts)) no longer matches and must be replaced. C(known_hosts_line) is
    the line to pin."
  - Runs on the host that can reach the device and drives ssh itself.
options:
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode), a read and a board that already matches need none.
    type: bool
    default: false
  route:
    description: The device's ssh route (C(bench_routes.shell)).
    type: dict
    required: true
  path:
    description: Where the private key file may be on the device, in order. The first that
      is there is the key, and the public key is beside it as C(<path>.pub). The family's to
      say (C(bench_host_key_paths)); a layout names one with C(bench_host_key_path).
    type: list
    elements: str
    required: true
  action:
    description: C(read) the key pair, or C(ensure) the device holds C(value).
    type: str
    choices: [read, ensure]
    default: read
  value:
    description: (ensure) The escrowed key pair, as C(action=read) returned it.
    type: str
  fingerprint:
    description: (ensure) The fingerprint the escrow records for C(value).
    type: str
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Read the host key for escrow
  rxrbench.linux.linux_host_key:
    route: "{{ bench_routes.shell }}"
    path: "{{ bench_host_key_paths }}"
  register: host_key
  no_log: true
"""

RETURN = r"""
fingerprint:
  description: The key's fingerprint as ssh shows it (C(SHA256:...)); after the write, in ensure mode.
  returned: success
  type: str
value:
  description: (read) The key pair. A secret.
  returned: action=read
  type: str
presents:
  description: (ensure, when written) Whether the device's ssh server presents the escrowed
    key on a new connection. False means it serves the old one until it restarts.
  returned: when changed
  type: bool
known_hosts_line:
  description: The line that pins the device's host key in a known_hosts file.
  returned: success
  type: str
"""

import os
import tempfile

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils import ssh
from ansible_collections.rxrbench.linux.plugins.module_utils import hostkey


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "route": {"type": "dict", "required": True},
            "path": {"type": "list", "elements": "str", "required": True},
            "action": {"type": "str", "choices": ["read", "ensure"], "default": "read"},
            "value": {"type": "str", "no_log": True},
            "fingerprint": {"type": "str"},
        },
        required_if=[("action", "ensure", ("value",))],
        supports_check_mode=True,
    )
    p = module.params
    try:
        target = ssh.Target(p["route"])
        path = hostkey.find(target, p["path"])
        have_fp, have = hostkey.read(target, path)
        line = hostkey.known_hosts_line(target.host, target.port, hostkey.unpack(have)[1])
        if p["action"] == "read":
            module.exit_json(changed=False, fingerprint=have_fp, value=have, known_hosts_line=line)
        want_fp = hostkey.fingerprint(hostkey.unpack(p["value"])[1])
        if p["fingerprint"] and p["fingerprint"] != want_fp:
            module.fail_json(msg=f"the escrow says {p['fingerprint']}, the key it holds is {want_fp}")
        if have == p["value"]:
            module.exit_json(changed=False, fingerprint=have_fp, known_hosts_line=line)
        result = {"changed": True, "fingerprint_before": have_fp, "fingerprint": want_fp,
                  "known_hosts_line": hostkey.known_hosts_line(
                      target.host, target.port, hostkey.unpack(p["value"])[1])}
        if module.check_mode:
            module.exit_json(msg=f"would give the device its escrowed host key: {have_fp} -> "
                                 f"{want_fp}", **result)
        gate.check(module)   # the layout's go-ahead, before the first write
        hostkey.write(target, path, p["value"])
        # Read it back on a connection that pins the escrowed key: if the server presents
        # it, that is the proof. If it still presents the old one, read through that.
        fd, pin = tempfile.mkstemp(prefix="rxrbench-pin-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(result["known_hosts_line"] + "\n")
            pinned = ssh.Target({**p["route"], "known_hosts": pin})
            result["presents"] = pinned.up()
            now_fp, now = hostkey.read(pinned if result["presents"] else target, path)
        finally:
            os.unlink(pin)
        if now != p["value"]:
            module.fail_json(msg=f"the device did not take the host key: it holds {now_fp}", **result)
    except (ssh.SshError, hostkey.HostKeyError) as e:
        module.fail_json(msg=str(e))
    module.exit_json(msg="the device presents its escrowed host key" if result["presents"] else
                     "the files are replaced; its ssh server presents the old key until it "
                     "restarts", **result)


if __name__ == "__main__":
    main()
