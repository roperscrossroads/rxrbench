#!/usr/bin/python
"""Read what a Linux device says it is, over ssh."""

DOCUMENTATION = r"""
module: linux_facts
short_description: What a Linux device says it is
description:
  - Reads a device's C(/etc/os-release), kernel, hostname and uptime over ssh, from the host
    this module runs on. Nothing runs on the device but a shell, so an image without Python
    is fine.
  - With C(declared), says which of the declared C(os-release) fields the device does not
    match. Nothing here can change what a device runs.
  - Never changes anything, and behaves the same in check mode.
options:
  route:
    description: The device's ssh route, as the inventory resolves it (C(bench_routes.shell)).
    type: dict
    required: true
  declared:
    description: C(os-release) fields the layout declares for the device, by their lower-case
      names (C(id), C(version_id), C(build_id), ...).
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Read the device
  rxrbench.linux.linux_facts:
    route: "{{ bench_routes.shell }}"
    declared: {id: poky, version_id: "5.0"}
  register: board_os
"""

RETURN = r"""
os:
  description: C(/etc/os-release) as a dict with lower-case keys.
  returned: success
  type: dict
hostname:
  description: The device's hostname.
  returned: success
  type: str
kernel:
  description: The running kernel (C(uname -r)).
  returned: success
  type: str
machine:
  description: The machine hardware name (C(uname -m)).
  returned: success
  type: str
uptime_s:
  description: Seconds since boot.
  returned: success
  type: int
differs:
  description: The declared fields the device does not match, each as C([device, declared]).
  returned: success
  type: dict
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import ssh
from ansible_collections.rxrbench.linux.plugins.module_utils import osinfo


def main():
    module = AnsibleModule(argument_spec={"route": {"type": "dict", "required": True},
                                          "declared": {"type": "dict", "default": {}}},
                           supports_check_mode=True)
    try:
        facts = osinfo.read(ssh.Target(module.params["route"]))
    except (ssh.SshError, ValueError) as e:
        module.fail_json(msg=str(e))
    module.exit_json(changed=False, differs=osinfo.differs(facts["os"], module.params["declared"]),
                     **facts)


if __name__ == "__main__":
    main()
