#!/usr/bin/python
"""Update a Linux device with a RAUC bundle, over ssh."""

DOCUMENTATION = r"""
module: rauc_install
short_description: Update a Linux device with a RAUC bundle
description:
  - Brings a RAUC-updated device to a bundle. The bundle is copied to the device, installed
    into the inactive slot, the device is rebooted, and it must come back running the bundle
    from the other slot. If it comes back on the old slot the update was rolled back, and
    the module fails saying so.
  - Decides by identity. A device whose booted slot already holds the bundle is not touched.
  - "Refuses, before anything is copied: a bundle whose C(compatible) is not the device's, a
    device whose running slot is not yet marked good, and a device without room for the bundle."
  - "Booting the bundle is not the end of an update. On images with a health gate, a unit soaks for
    some minutes and only then lets the slot be marked good; a slot that never passes is rolled
    back by the bootloader once its boot attempts are used up. C(health) in the result is
    C(healthy), C(soaking), C(failed), C(unknown) (the device has no such unit) or C(none)
    (no C(health_unit) given: the image has no gate). With C(wait_good) the
    module waits for the gate, and fails if the gate fails or the device is found back on
    the slot it was updated from."
  - Runs on the host that can reach the device, and needs C(rauc) there to read the bundle.
  - Supports check mode and diff.
options:
  board:
    description: The device's registry name. The route's address is checked against the
      registry on this host (C(devreg guard --route)) before any connection is made.
    type: str
    required: true
  devreg:
    description: The devreg script on this host.
    type: path
    default: ~/.devreg/devreg.py
  route:
    description: The device's ssh route (C(bench_routes.flash)).
    type: dict
    required: true
  bundle:
    description: The signed bundle (C(.raucb)), on the host this module runs on.
    type: path
    required: true
  compatible:
    description: The C(compatible) the device registry records for the device.
    type: str
  reboot_timeout:
    description: Seconds the device has to come back after the reboot.
    type: float
    default: 240.0
  wait_good:
    description: Seconds to wait for the new slot to pass its health gate. 0 does not wait.
    type: float
    default: 0.0
  remote_bundle:
    description: Where the bundle lands on the device before C(rauc install); its directory
      must have room. The family's to say (C(bench_remote_bundle)); there is no standard place.
    type: str
    required: true
  health_unit:
    description: The systemd unit that gates a booted slot as healthy, on images that have
      one (C(bench_health_unit), the family's). Without it a slot's health is RAUC's own
      C(boot_status).
    type: str
  delivery:
    description:
      - A route that carries the bundle to the device itself (C(bench_delivery), from the
        plugin that brings the route). The bundle is then not copied or installed from here.
        The route is told which bundle the device should take, and this module waits for the
        device to fetch it, install it and reboot.
      - The device is read over ssh before and after all the same (I(route) is then its ssh
        route), so what the route's own records say never decides whether the update worked.
    type: dict
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
        required: true
      address:
        description: The device's address on that route, as the registry records it.
        type: str
      settings:
        description: The plugin's own settings for the route (its server, its login).
        type: dict
      credential:
        description: What the device shows the route when it asks, when the layout declares it.
        type: str
  delivery_timeout:
    description: Seconds a device has to take a delivery and go down for its reboot. It asks
      on its own schedule, so this is that interval plus the download and the install.
    type: float
    default: 1800.0
  family:
    description: The device's family, which its identity names.
    type: str
    default: linux_rauc
  flash_unknown:
    description: Write a board that answers as its family but reports neither a build nor
      a hash, so nothing can tell whether it already runs the image. On by default (a roll
      from old firmware needs it); a layout turns it off with C(bench_flash_unknown), and
      such a board is then refused.
    type: bool
    default: true
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode) needs none.
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
extends_documentation_fragment:
  - rxrbench.core.check_depth
"""

EXAMPLES = r"""
- name: The declared bundle
  rxrbench.linux.rauc_install:
    board: "{{ bench_board }}"
    route: "{{ bench_routes.flash }}"
    bundle: "{{ bench_bundle }}"
    compatible: "{{ bench_compatible }}"
"""

RETURN = r"""
image:
  description: What the bundle is.
  returned: always
  type: dict
device:
  description: What the device runs after the run (in check mode, what it runs now).
  returned: success
  type: dict
slot:
  description: The booted slot after the run.
  returned: success
  type: dict
health:
  description: The running slot's health gate (C(healthy), C(soaking), C(failed), C(unknown)).
  returned: success
  type: str
pending:
  description: True while the running slot has not passed its health gate.
  returned: success
  type: bool
timing:
  description: Each phase of the run (resolve, read, preflight, write, verify, leave) and its duration in seconds, as C(module_utils/timing.py) records it.
  returned: success
  type: list
  elements: dict
checked:
  description: The depth of the checks before the write (C(full) or C(spot)) and why (docs/ARCHITECTURE.md#checking-depth).
  returned: success
  type: dict
delivery:
  description: With I(delivery), the route's own record beside the device's answer. C(bearer),
    C(address), C(started) (the delivery this run started, absent when nothing was written),
    C(route_says) (what the route knows of the device after the run) and C(agrees) (whether
    that matches what the device itself runs; false is reported, and the device's answer stands).
  returned: when I(delivery) is given
  type: dict
"""

import json
import os
import subprocess

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import ssh
from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from ansible_collections.rxrbench.core.plugins.module_utils.flash import delivery as route_delivery
from ansible_collections.rxrbench.linux.plugins.module_utils import rauc


def bundle_manifest(path, family="linux_rauc"):
    """A manifest for a bundle, from the bundle itself (`rauc info`)."""
    try:
        r = subprocess.run(["rauc", "info", "--no-verify", "--output-format=json", path],
                           capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise common.FlashError(f"cannot run rauc on this host: {e}") from e
    if r.returncode != 0:
        raise common.FlashError(f"{path}: not a readable RAUC bundle: {r.stderr.strip()[-200:]}")
    ident = rauc.of_bundle(json.loads(r.stdout), family)
    return {"family": family, "format": "raucb", "file": os.path.basename(path),
            **{k: v for k, v in ident.items() if v is not None and k != "family"}}


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "route": {"type": "dict", "required": True},
            "devreg": {"type": "path", "default": "~/.devreg/devreg.py"},
            "bundle": {"type": "path", "required": True},
            "compatible": {"type": "str"},
            "reboot_timeout": {"type": "float", "default": 240.0},
            "wait_good": {"type": "float", "default": 0.0},
            "remote_bundle": {"type": "str", "required": True},
            "health_unit": {"type": "str"},
            "family": {"type": "str", "default": "linux_rauc"},
            "flash_unknown": {"type": "bool", "default": True},
            "check_depth": {"type": "str", "choices": ["full", "spot"], "default": "full"},
            "delivery": {"type": "dict", "options": {
                "bearer": {"type": "str", "required": True},
                "tool": {"type": "str", "required": True},
                "formats": {"type": "list", "elements": "str", "required": True},
                "address": {"type": "str"},
                "settings": {"type": "dict", "no_log": True},
                "credential": {"type": "str", "no_log": True},
            }},
            "delivery_timeout": {"type": "float", "default": 1800.0},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    carrier = flasher = None
    try:
        if not os.path.isfile(p["bundle"]):
            raise common.FlashError(f"{p['bundle']}: no such bundle")
        manifest = bundle_manifest(p["bundle"], p["family"])
        target = ssh.Target(p["route"])
        # The address is the registry's for this device, or no connection is made.
        why = common.route_refusal(p["devreg"], p["board"],
                                   p["route"].get("bearer") or "ssh", target.host)
        if why:
            raise common.FlashError(why)
        if p["delivery"]:
            # The device's address on the delivering route is the registry's too, or the
            # route is not told anything about it.
            why = common.route_refusal(p["devreg"], p["board"], p["delivery"]["bearer"],
                                       p["delivery"].get("address"))
            if why:
                raise common.FlashError(why)
            carrier = route_delivery.Delivery(p["delivery"], p["delivery"].get("address"))
        flasher = rauc.RaucFlasher(target, manifest, p["bundle"], p["remote_bundle"],
                                   compatible=p["compatible"],
                                   reboot_timeout=p["reboot_timeout"], delivery=carrier,
                                   delivery_timeout=p["delivery_timeout"])
        r = common.drive(flasher, manifest, check_mode=module.check_mode, check_depth=p["check_depth"],
                             flash_unknown=p["flash_unknown"])
        state = rauc.health(target, p["health_unit"])
        if r["changed"] and not module.check_mode and p["wait_good"] > 0:
            left = flasher.before_slot["bootname"] if flasher.before_slot else None
            flasher.slot, state = rauc.wait_healthy(target, left, p["wait_good"],
                                                    unit=p["health_unit"], family=p["family"])
    except (common.FlashError, ssh.SshError, ValueError) as e:
        msg = str(e)
        if carrier is not None and flasher is not None and flasher.delivered is not None:
            msg += "; " + carrier.verdict()      # what the route recorded of a delivery that failed
        module.fail_json(msg=msg)
    result = {"changed": r["changed"], "image": r["image"],
              "device": r["before"] if module.check_mode else r["after"],
              "slot": flasher.slot, "health": state,
              "pending": state == "soaking" or flasher.slot["boot_status"] != "good"}
    if carrier is not None:
        result["delivery"] = carrier.record(flasher.delivered, r["image"],
                                            r["before"] if module.check_mode else r["after"])
    result["timing"] = r.get("timing", [])
    result["checked"] = r.get("checked")
    result["identity_checked"] = r.get("identity_checked")
    if r["changed"]:
        result["why"] = r["why"]
        result["diff"] = {"before": {k: v for k, v in r["before"].items() if v is not None},
                          "after": {k: v for k, v in r["after"].items() if v is not None}}
    module.exit_json(**result)


if __name__ == "__main__":
    main()
