#!/usr/bin/python
"""The images a Launcher board declares, as files: fetched, pinned, described."""

DOCUMENTATION = r"""
module: launcher_images
short_description: Resolve a Launcher board's declared images to files on this machine
description:
  - For the Launcher image a board declares (C(firmware.image), a catalog image of kind
    C(manager)) and each app it declares (C(launcher.apps.<label>.image), kind C(app)), fetches
    the source the firmware catalog records (a path as it is; a remote source once, into the
    cache, refused when its bytes are not the pinned sha256) and says what each file is (size,
    sha256, the app part Launcher installs). Runs on the controller; touches no device.
options:
  manager:
    description: The catalog name of Launcher's own image, or none.
    type: str
  apps:
    description: Slot label to C(image), a catalog name.
    type: dict
    default: {}
  images:
    description: The firmware catalog's images (C(lookup('rxrbench.core.bench_plugins', what='images'))).
    type: dict
    required: true
  cache_dir:
    description: Where remote sources are kept.
    type: path
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: The declared images
  rxrbench.launcher.launcher_images:
    manager: "{{ firmware.image }}"
    apps: "{{ launcher.apps | default({}) }}"
    images: "{{ lookup('rxrbench.core.bench_plugins', what='images') }}"
  delegate_to: localhost
"""

RETURN = r"""
manager:
  description: Launcher's image, C({path, size, sha256, app_offset, app_size, version, image}), or null.
  returned: always
  type: dict
apps:
  description: By slot label (path, size, sha256, app_offset, app_size, image, version).
  returned: always
  type: dict
files:
  description: Every file, for staging.
  returned: always
  type: list
"""

import os

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import artifact
from ansible_collections.rxrbench.launcher.plugins.module_utils import images


#: The family whose images this module resolves (what the catalog entry must say).
FAMILY = "launcher"


def resolve(name, catalog, kind, cache_dir):
    img = catalog.get(name)
    if img is None:
        raise artifact.ArtifactError(f"image {name!r} is not in the firmware catalog")
    if img.get("family") != FAMILY:
        raise artifact.ArtifactError(f"image {name} is {img.get('family')} firmware, not {FAMILY}")
    if img.get("kind", "manager") != kind:
        raise artifact.ArtifactError(f"image {name} is of kind {img.get('kind', 'manager')}, wanted {kind}")
    path = artifact.fetch(img.get("source"), cache_dir=cache_dir)
    if os.path.isdir(path):
        raise artifact.ArtifactError(f"image {name}: its source is a directory; a Launcher image is one file")
    d = images.describe(path)
    if kind == "manager" and not d["manager"]:
        raise artifact.ArtifactError(f"image {name} carries no app manager's table: not a Launcher release")
    return {**d, "image": name, "version": img.get("version")}


def main():
    module = AnsibleModule(argument_spec={
        "manager": {"type": "str"},
        "apps": {"type": "dict", "default": {}},
        "images": {"type": "dict", "required": True},
        "cache_dir": {"type": "path"},
    }, supports_check_mode=True)
    p = module.params
    out = {"manager": None, "apps": {}, "files": []}
    try:
        if p["manager"]:
            out["manager"] = resolve(p["manager"], p["images"], "manager", p["cache_dir"])
            out["files"].append(out["manager"]["path"])
        for label, app in (p["apps"] or {}).items():
            name = (app or {}).get("image")
            if not name:
                module.fail_json(msg=f"launcher.apps.{label} names no image")
            out["apps"][label] = resolve(name, p["images"], "app", p["cache_dir"])
            out["files"].append(out["apps"][label]["path"])
    except (artifact.ArtifactError, images.ImageError, OSError) as e:
        module.fail_json(msg=str(e))
    module.exit_json(changed=False, **out)


if __name__ == "__main__":
    main()
