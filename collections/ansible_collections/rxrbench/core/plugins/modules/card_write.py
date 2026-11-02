#!/usr/bin/python
"""Write a disk image to a registered card."""

DOCUMENTATION = r"""
module: card_write
short_description: Write a disk image to a registered removable card
description:
  - Writes a whole disk image (a Yocto C(.wic) with its C(.bmap)) to a card in a registered
    reader, using the site's card-writing kit, which guards the target, writes with
    bmaptool and reads the written blocks back.
  - "The target is named, never a path: it must be a device the registry lists as
    C(kind: block) and C(destructive: true). C(devreg) resolves it and C(devreg guard) vouches
    for the path before the kit sees it."
  - C(confirm) must repeat the device's name. It replaces the kit's typed confirmation, so it
    should come from a person's command line, not from a layout file.
  - This erases the card, including a node's identity and config on it. A device that is
    running is updated by its family's own update step, not with this.
  - A card cannot be asked what it holds, so a run always writes and reports C(changed).
    In check mode every check is made and nothing is written.
  - Runs on the workstation that has the reader. Needs passwordless sudo for the kit;
    without it nothing is written and the module says to run the write attended.
options:
  device:
    description: The registry name of the card reader or card.
    type: str
    required: true
  image:
    description: The image directory (C(manifest.json), the C(.wic) and its C(.bmap)).
    type: path
    required: true
  confirm:
    description: The device's name again.
    type: str
  compatible:
    description: The C(compatible) of the device the card is for. The image must be for it.
    type: str
  kit:
    description: The card-writing kit's C(write-card.sh) (a site setting, C(RXRBENCH_CARD_KIT)).
    type: path
  registry:
    description: The device registry. Unset, it is C(DEVREG_REGISTRY), else the copy C(just hosts-setup)
      deploys to a bench host (C(~/.devreg/registry.yaml)).
    type: path
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode) needs none.
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Write the card
  rxrbench.core.card_write:
    device: sdreader
    image: "{{ bench_card_image }}"
    compatible: "{{ bench_compatible }}"
    confirm: "{{ card_confirm | default(omit) }}"
"""

RETURN = r"""
path:
  description: The device path that was (or, in check mode, would be) written.
  returned: success
  type: str
image:
  description: What the image is.
  returned: success
  type: dict
"""

import os

import yaml
from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils.flash import card, common
from ansible_collections.rxrbench.core.plugins.module_utils import gate


def main():
    module = AnsibleModule(
        argument_spec={
            "device": {"type": "str", "required": True},
            "image": {"type": "path", "required": True},
            "confirm": {"type": "str"},
            "compatible": {"type": "str"},
            "kit": {"type": "path", "default": os.environ.get("RXRBENCH_CARD_KIT")},
            "registry": {"type": "path"},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    try:
        manifest, image_file = common.load_image(p["image"])
        registry = (p["registry"] or os.environ.get("DEVREG_REGISTRY")
                    or "~/.devreg/registry.yaml")
        with open(os.path.expanduser(registry)) as f:
            entry = (yaml.safe_load(f).get("devices") or {}).get(p["device"])
        if entry is None:
            raise common.FlashError(f"{p['device']} is not in the device registry")
        writer = card.CardWriter(p["device"], entry, manifest, image_file, p["kit"],
                                 confirm=p["confirm"], compatible=p["compatible"],
                                 planning=module.check_mode)
        refusals = writer.preflight()
        if refusals:
            raise common.FlashError("refused: " + "; ".join(refusals))
        if module.check_mode:
            module.exit_json(changed=True, path=writer.path,
                             image=common.image_identity(manifest),
                             msg=f"every check passed: {os.path.basename(image_file)} would be "
                                 f"written to {writer.path}, erasing it. To write, confirm "
                                 f"with the device's name ({p['device']}).")
        writer.write()
    except (common.FlashError, OSError, ValueError, yaml.YAMLError) as e:
        module.fail_json(msg=str(e))
    module.exit_json(changed=True, path=writer.path, image=common.image_identity(manifest))


if __name__ == "__main__":
    main()
