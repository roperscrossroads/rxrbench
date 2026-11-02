#!/usr/bin/python
"""Read what a RAUC-updated Linux device runs."""

DOCUMENTATION = r"""
module: rauc_facts
short_description: What a RAUC-updated Linux device runs
description:
  - Reads a device's RAUC status over ssh, from the host this module runs on. Nothing runs on
    the device but C(rauc) and a shell, so an image without Python is fine.
  - Never changes anything, and behaves the same in check mode.
options:
  route:
    description: The device's ssh route, as the inventory resolves it (C(bench_routes.shell)).
    type: dict
    required: true
  health_unit:
    description: The systemd unit that gates a booted slot as healthy, on images that have
      one (C(bench_health_unit), the family's). Without it C(slot.health) is C(none).
    type: str
  family:
    description: The device's family, which its identity names.
    type: str
    default: linux_rauc
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Read the device
  rxrbench.linux.rauc_facts:
    route: "{{ bench_routes.shell }}"
  register: bench_actual
"""

RETURN = r"""
device:
  description: The identity of the booted slot (family, target, version, hash).
  returned: success
  type: dict
slot:
  description: The booted slot (bootname, boot_status, device).
  returned: success
  type: dict
os:
  description: C(/etc/os-release) as a dict, and the hostname.
  returned: success
  type: dict
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import ssh
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from ansible_collections.rxrbench.linux.plugins.module_utils import rauc


def main():
    module = AnsibleModule(argument_spec={"route": {"type": "dict", "required": True},
                                          "health_unit": {"type": "str"},
                                          "family": {"type": "str", "default": "linux_rauc"}},
                           supports_check_mode=True)
    try:
        target = ssh.Target(module.params["route"])
        ident, slot = rauc.read_device(target, module.params["family"])
        slot["health"] = rauc.health(target, module.params["health_unit"])
        osr = {}
        for line in target.check("cat /etc/os-release; echo HOSTNAME=$(hostname)").splitlines():
            k, sep, v = line.partition("=")
            if sep:
                osr[k.strip().lower()] = v.strip().strip('"')
    except (ssh.SshError, common.FlashError) as e:
        module.fail_json(msg=str(e))
    module.exit_json(changed=False, device=ident, slot=slot, os=osr)


if __name__ == "__main__":
    main()
