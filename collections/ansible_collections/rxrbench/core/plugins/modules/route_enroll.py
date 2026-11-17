#!/usr/bin/python
"""Make a route that delivers images know a device, with the credential its layout declares."""

DOCUMENTATION = r"""
module: route_enroll
short_description: Enroll a device on a route that delivers its image
description:
  - A route that carries an image to the device itself (C(bench_delivery), a plugin's
    delivering route) may hold a credential for each device, which the device shows when it
    asks the route for its image. With one declared, this module asks the route whether it
    holds the device with it, and makes it so when it does not. The device is made known to
    the route when the route has none of that address, and its credential is set when the
    route holds another.
  - The device itself is not touched. Its own copy of the credential is part of its config,
    declared in its family's vocabulary.
  - The device's address on the route is checked against the registry first.
  - Supports check mode. The plan says what would be done and nothing is changed.
options:
  board:
    description: The board's name.
    type: str
    required: true
  devreg:
    description: The device registry tool on this host.
    type: path
    default: ~/.devreg/devreg.py
  delivery:
    description: The route (C(bench_delivery)), with the plugin's settings and the credential.
    type: dict
    required: true
    suboptions:
      bearer:
        description: The route's name in the registry.
        type: str
        required: true
      tool:
        description: The plugin's tool on this host that speaks for the route.
        type: str
        required: true
      formats:
        description: The image formats the route carries.
        type: list
        elements: str
      address:
        description: The device's address on that route, as the registry records it.
        type: str
      settings:
        description: The plugin's own settings for the route (its server, its login).
        type: dict
      credential:
        description: What the device shows the route when it asks.
        type: str
        required: true
  apply_allowed:
    description: The layout's go-ahead for writing (C(bench_apply_allowed)).
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: The device's place on the route that delivers its image
  rxrbench.core.route_enroll:
    board: "{{ bench_board }}"
    delivery: "{{ bench_delivery | combine({'settings': bench_updates, 'credential': bench_updates_token}) }}"
    apply_allowed: "{{ bench_apply_allowed | default(false) | bool }}"
  no_log: true
"""

RETURN = r"""
step:
  description: What differed, in words, or an empty string. It never carries the credential.
  type: str
  returned: always
"""

import os  # noqa: E402

from ansible.module_utils.basic import AnsibleModule  # noqa: E402

from ansible_collections.rxrbench.core.plugins.module_utils import gate  # noqa: E402
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common  # noqa: E402
from ansible_collections.rxrbench.core.plugins.module_utils.flash import delivery as route_delivery  # noqa: E402


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "devreg": {"type": "path", "default": "~/.devreg/devreg.py"},
            "delivery": {"type": "dict", "required": True, "options": {
                "bearer": {"type": "str", "required": True},
                "tool": {"type": "str", "required": True},
                "formats": {"type": "list", "elements": "str"},
                "address": {"type": "str"},
                "settings": {"type": "dict", "no_log": True},
                "credential": {"type": "str", "required": True, "no_log": True},
            }},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before the route is told anything
    spec = p["delivery"]
    try:
        why = common.route_refusal(os.path.expanduser(p["devreg"]), p["board"], spec["bearer"],
                                   spec.get("address"))
        if why:
            raise common.FlashError(why)
        carrier = route_delivery.Delivery(spec, spec.get("address"))
        step = carrier.enrollment()
        if step and not module.check_mode:
            carrier.enroll()
            left = carrier.enrollment()
            if left:
                raise common.FlashError(f"{p['board']}: enrolled, and still: {left}")
    except common.FlashError as e:
        module.fail_json(msg=str(e), step="")
    module.exit_json(changed=bool(step), step=step or "")


if __name__ == "__main__":
    main()
