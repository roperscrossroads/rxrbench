#!/usr/bin/python
"""Read what a chip behind an SWD probe is, without changing it."""

DOCUMENTATION = r"""
module: swd_identify
short_description: Read a chip's identity through its SWD probe, without halting it
description:
  - Reads the identity of a chip wired to a debug probe on the bench host (its unique id, its
    device id, its flash size, the target voltage) with C(swd_chip.py id --look), which neither
    halts nor resets the core. What C(just diag) asks a board with no console.
  - The probe is the one the registry names, found by C(devreg) and handed to openocd by its USB
    location; the session stops if the chip's unique id is not C(uid).
  - A chip declared C(nrst) is reached only by connecting under reset, which resets it; it is
    not read, and the result says so.
  - Changes nothing. Supports check mode.
options:
  board:
    description: The device's name.
    type: str
    required: true
  probe:
    description: The probe's registry name.
    type: str
    required: true
  chip:
    description: The part, for example C(STM32F767ZI). It picks the debug recipe.
    type: str
    required: true
  uid:
    description: The chip's unique id (the registry's C(reach.swd.uid)).
    type: str
    required: true
  nrst:
    description: The probe's reset line is wired and sessions connect under reset.
    type: bool
    default: false
  tools:
    description: Where the bench host keeps its tools. Keys C(swd_chip), C(devreg).
    type: dict
    default: {}
author: rxrbench
"""

RETURN = r"""
results:
  description: One result in the shape C(device_run) gives, so diag keeps it the same way.
  type: list
  elements: dict
  returned: always
  contains:
    name:
      description: C(swd-id).
      type: str
    command:
      description: What was run.
      type: str
    output:
      description: The identity as JSON, or why it was not read.
      type: str
    ok:
      description: The chip was read.
      type: bool
"""

import json

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, swd


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "probe": {"type": "str", "required": True},
            "chip": {"type": "str", "required": True},
            "uid": {"type": "str", "required": True},
            "nrst": {"type": "bool", "default": False},
            "tools": {"type": "dict", "default": {}},
        },
        supports_check_mode=True,
    )
    p = module.params
    result = {"name": "swd-id", "command": "swd_chip.py id --look", "ok": False}
    if p["nrst"]:
        result["output"] = (f"{p['board']}: reached only by connecting under reset (nrst), which "
                            "resets it; diag changes nothing, so it was not read")
    else:
        tools = {k: v for k, v in p["tools"].items() if v and k in ("swd_chip", "devreg")}
        probe = swd.Probe(p["board"], p["probe"], p["chip"], p["uid"], False, **tools)
        try:
            got = probe.call("id", "--look", timeout=120)
            result.update(ok=True, output=json.dumps(got, indent=2, sort_keys=True))
        except (common.FlashError, OSError) as e:
            result["output"] = str(e)
    module.exit_json(changed=False, results=[result])


if __name__ == "__main__":
    main()
