#!/usr/bin/python
"""The fixture plugin's probe chip: its flash is a file standing in for a chip behind a debug
probe (core-portability P6). A plugin's own module on core's code: the image read with
common.load_image (core's raw-bin, an Intel HEX or ELF file staged with its address, and this
plugin's own `probe-img` format), written through common.drive with a flasher core does not
have, and the chip read, written back and erased whole. Its config regions (its declared ranges
with role config) are backed up, written back and erased through core's region flow, with a
route of this plugin's own (docs/ARCHITECTURE.md#chip-and-region-flows)."""
import hashlib
import os
import shutil

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import identity
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, region_flow

DOCUMENTATION = r"""
module: probe_chip
short_description: The fixture plugin's probe chip, a file standing in for its flash
description:
  - C(flash) writes an image where it is linked (C(base), else the start of flash) and decides
    by the hash of the flash there; C(backup), C(restore) and C(erase) read, write back and
    erase the whole flash; C(region_backup), C(region_restore) and C(region_erase) do so for its
    config regions, through core's region flow. Supports check mode.
options:
  board:
    description: The device's name.
    type: str
    required: true
  chip:
    description: The file that stands in for the chip's flash (absent, the chip does not answer).
    type: path
    required: true
  action:
    description: What to do.
    type: str
    choices: [flash, backup, restore, erase, region_backup, region_restore, region_erase]
    default: flash
  image:
    description: The image directory (C(flash)).
    type: path
  path:
    description: The backup directory (C(backup), C(restore)).
    type: path
  flash_regions:
    description: The chip's declared ranges (C(bench_flash_regions)); core's write rule reads
      them through this module's read (C(flash)).
    type: list
    elements: dict
    default: []
  overwrite:
    description: The ranges a write may take (C(bench_overwrite)).
    type: list
    elements: str
    default: []
  dest:
    description: Where region backups go (C(region_backup), C(region_restore)).
    type: path
  backup:
    description: Which region backup to write back (C(latest), a name or a path).
    type: str
    default: latest
  region:
    description: The config region to erase (default the first).
    type: str
  apply_allowed:
    description: The layout's go-ahead (a region write or erase).
    type: bool
    default: false
author: rxrbench tests
"""

#: Where the fixture chip's flash starts, and its formats besides core's.
FLASH_START = 0x08000000
FORMATS = ("probe-img",)


class ProbeFlasher(common.Flasher):
    def __init__(self, chip, manifest, image_file):
        self.chip, self.manifest = chip, manifest
        with open(image_file, "rb") as f:
            self.data = f.read()
        self.at = int(str(manifest.get("base") or FLASH_START), 0) - FLASH_START

    def device(self):
        if not os.path.exists(self.chip):
            raise common.Unanswered(f"{self.chip}: the probe sees no chip")
        with open(self.chip, "rb") as f:
            f.seek(self.at)
            held = f.read(len(self.data))
        return identity.make("probechip", hash=hashlib.sha256(held).hexdigest())

    def preflight(self):
        out = common.common_refusals(self.manifest, ("raw-bin",) + FORMATS, at_base=True)
        size = os.path.getsize(self.chip)
        if self.at < 0 or self.at + len(self.data) > size:
            out.append(f"the image goes at {self.at + FLASH_START:#x}, {len(self.data)} bytes: "
                       f"outside the chip's {size}-byte flash")
        return out

    def read_range(self, start, length):
        """Bytes of the chip at an absolute address (core's write rule reads region heads)."""
        if not os.path.exists(self.chip):
            raise common.Unanswered(f"{self.chip}: the probe sees no chip")
        with open(self.chip, "rb") as f:
            f.seek(max(0, start - FLASH_START))
            return f.read(length)

    def write_ranges(self):
        return [(self.at + FLASH_START, len(self.data))]

    def write(self):
        with open(self.chip, "r+b") as f:
            f.seek(self.at)
            f.write(self.data)

    def verify(self):
        return self.device()


class _FileSession(region_flow.Session):
    """The chip file, at its addresses: nothing to enter, nothing to come back from."""

    def __enter__(self):
        if not os.path.exists(self.route.p["chip"]):
            raise common.Unanswered(f"{self.route.p['chip']}: the probe sees no chip")
        return self

    def _at(self, offset):
        return offset - FLASH_START

    def read(self, regions, out):
        with open(self.route.p["chip"], "rb") as f:
            for name, off, size in regions:
                f.seek(self._at(off))
                with open(os.path.join(out, f"{name}.bin"), "wb") as o:
                    o.write(f.read(size))

    def write(self, parts, meta):
        with open(self.route.p["chip"], "r+b") as f:
            for x in parts:
                with open(x["file"], "rb") as i:
                    f.seek(self._at(x["offset"]))
                    f.write(i.read())

    def erase(self, name, offset, size):
        with open(self.route.p["chip"], "r+b") as f:
            f.seek(self._at(offset))
            f.write(b"\xff" * size)


class FileRoute(region_flow.Route):
    """This plugin's route to the probe chip's config regions, on core's flow."""
    name = "probefile"

    def chip(self):
        return {"chip_id": self.p["board"]}

    def session(self, verb, regions, name=None):
        return _FileSession(self, verb)


def regions(module, p):
    """The region verbs, on core's flow (each ends the module run)."""
    ranges = region_flow.declared_regions(p["flash_regions"])
    if p["action"] == "region_backup":
        region_flow.backup(module, p, FileRoute(p), ranges)
    elif p["action"] == "region_restore":
        region_flow.restore(module, p, FileRoute(p),
                            region_flow.backup_path(p["dest"], p["board"], p["backup"]), ranges)
    else:
        region_flow.erase(module, p, FileRoute(p), ranges, p["region"] or ranges[0][0])


def main():
    module = AnsibleModule(argument_spec={
        "board": {"type": "str", "required": True},
        "chip": {"type": "path", "required": True},
        "action": {"type": "str", "choices": ["flash", "backup", "restore", "erase", "region_backup",
                                              "region_restore", "region_erase"], "default": "flash"},
        "image": {"type": "path"},
        "path": {"type": "path"},
        "flash_regions": {"type": "list", "elements": "dict", "default": []},
        "overwrite": {"type": "list", "elements": "str", "default": []},
        "dest": {"type": "path"},
        "backup": {"type": "str", "default": "latest"},
        "region": {"type": "str"},
        "apply_allowed": {"type": "bool", "default": False},
    }, supports_check_mode=True)
    p = module.params
    try:
        if p["action"].startswith("region_"):
            regions(module, p)
        if p["action"] == "flash":
            manifest, image_file = common.load_image(p["image"], formats=FORMATS)
            flasher = ProbeFlasher(p["chip"], manifest, image_file)
            flasher.flash_regions, flasher.overwrite = p["flash_regions"], tuple(p["overwrite"])
            r = common.drive(flasher, manifest, check_mode=module.check_mode)
            result = {"changed": r["changed"], "image": r["image"],
                      "device": r["before"] if module.check_mode else r["after"],
                      "checked": r.get("checked"), "identity_checked": r.get("identity_checked")}
            if r["changed"]:
                result["why"] = r["why"]
                result["diff"] = {"before": {k: v for k, v in r["before"].items() if v is not None},
                                  "after": {k: v for k, v in r["after"].items() if v is not None}}
            module.exit_json(**result)
        if not os.path.exists(p["chip"]):
            raise common.Unanswered(f"{p['chip']}: the probe sees no chip")
        size = os.path.getsize(p["chip"])
        if p["action"] == "backup":
            os.makedirs(p["path"], exist_ok=True)
            shutil.copyfile(p["chip"], os.path.join(p["path"], "flash.bin"))
            module.exit_json(changed=False, chip={"mac": None, "flash_size": f"{size >> 20}MB"},
                             files=["flash.bin"])
        if p["action"] == "restore":
            src = os.path.join(p["path"], "flash.bin")
            with open(src, "rb") as a, open(p["chip"], "rb") as b:
                same = a.read() == b.read()
            if not same and not module.check_mode:
                shutil.copyfile(src, p["chip"])
            module.exit_json(changed=not same, msg="written back through the probe" if not same
                             else "the chip already holds the backup")
        if not module.check_mode:
            with open(p["chip"], "wb") as f:
                f.write(b"\xff" * size)
        module.exit_json(changed=True, msg="erased through the probe")
    except common.FlashError as e:
        module.fail_json(msg=str(e), unanswered=isinstance(e, common.Unanswered))


if __name__ == "__main__":
    main()
