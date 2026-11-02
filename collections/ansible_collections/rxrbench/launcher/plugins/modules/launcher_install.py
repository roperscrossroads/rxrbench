#!/usr/bin/python
"""Put Launcher on an ESP32 that has none: its merged release image, whole, at 0x0."""

DOCUMENTATION = r"""
module: launcher_install
short_description: Write Launcher's merged image at 0x0 on a chip that has no Launcher yet
description:
  - The one write of a Launcher release at 0x0 (bootloader, its shipped partition table and
    Launcher itself), on a chip that carries no app manager. The chip is erased whole first (what
    was there is other firmware, by the layout's word). Refused on a chip whose partition table
    already shows an app manager (every installed app would be orphaned) unless
    C(app_manager_overwrite) says so; refused without C(first_flash), the layout's word that the
    board does not run what it declares; refused unless the survey facts show secure boot and
    flash encryption off and ROM download on.
  - Afterwards Launcher's console must answer with the declared version; success is what the
    board says, not what esptool printed.
options:
  board:
    description: The board's name (its broker, its registry entry).
    type: str
    required: true
  socket:
    description: The broker's socket, when not the default for the board.
    type: path
  image:
    description: The merged Launcher image on this host.
    type: path
    required: true
  version:
    description: The version Launcher must report afterwards.
    type: str
  chip:
    description: The board's chip, as esptool names it (C(bench_mcu)).
    type: str
    required: true
  tools:
    description: Where the bench host keeps its tools. Keys C(esptool), C(devreg).
    type: dict
    default: {}
  facts_tags:
    description: The board's survey tags (security state).
    type: list
    elements: str
    default: []
  first_flash:
    description: The layout's word (C(bench_first_flash)) that this board may be erased and written whole.
    type: bool
    default: false
  app_manager_overwrite:
    description: The layout's go-ahead (C(bench_app_manager_overwrite)) to write over an app manager already on the chip.
    type: bool
    default: false
  wait:
    description: Seconds to wait for Launcher's console afterwards.
    type: float
    default: 30.0
  entry:
    description: How Launcher's boot banner is answered after the write, C(selpress) or
      C(settings) (the board type's C(launcher_enter)).
    type: str
    choices: [selpress, settings]
    default: selpress
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)).
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Launcher onto a board that runs something else
  rxrbench.launcher.launcher_install:
    board: "{{ bench_board }}"
    image: "{{ board_images.manager.path }}"
    version: "{{ board_images.manager.version }}"
    chip: "{{ bench_mcu }}"
    first_flash: "{{ bench_first_flash | default(false) | bool }}"
    apply_allowed: "{{ bench_apply_allowed | default(false) | bool }}"
"""

RETURN = r"""
version:
  description: What Launcher answered afterwards.
  returned: when written
  type: str
image:
  description: What the image is (size, sha256, the app part).
  returned: always
  type: dict
"""

import os

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, gate
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from ansible_collections.rxrbench.launcher.plugins.module_utils import console, images

REQUIRED_TAGS = ("secure-boot:off", "flash-encryption:off", "rom-download:on")


def refusals(p, image):
    out = []
    if not p["first_flash"]:
        out.append("writing Launcher at 0x0 erases the chip: declare bench_first_flash: true for this "
                   "board if that is meant")
    missing = [t for t in REQUIRED_TAGS if t not in (p["facts_tags"] or ())]
    if missing:
        out.append("the survey facts do not show " + ", ".join(missing) + ": run `just survey <host> "
                   "true` for this board first")
    if not image["manager"]:
        out.append(f"{p['image']} is not a Launcher release image (no app manager in its table)")
    why = common.manager_tag_refusal(p["facts_tags"], p["app_manager_overwrite"], "writing at 0x0")
    if why:
        out.append(why)
    return out


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "socket": {"type": "path"},
            "image": {"type": "path", "required": True},
            "version": {"type": "str"},
            "chip": {"type": "str", "required": True},
            "tools": {"type": "dict", "default": {}},
            "facts_tags": {"type": "list", "elements": "str", "default": []},
            "first_flash": {"type": "bool", "default": False},
            "app_manager_overwrite": {"type": "bool", "default": False},
            "wait": {"type": "float", "default": 30.0},
            "entry": {"type": "str", "choices": list(console.ENTRIES), "default": "selpress"},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    esptool = os.path.expanduser(p["tools"].get("esptool") or "~/.venv-esp/bin/esptool")
    devreg = os.path.expanduser(p["tools"].get("devreg") or "~/.devreg/devreg.py")
    try:
        image = images.describe(p["image"])
    except (OSError, images.ImageError) as e:
        module.fail_json(msg=f"{p['image']}: {e}")
    result = {"changed": False, "image": {k: v for k, v in image.items() if k != "path"}}
    refused = refusals(p, image)
    if refused:
        module.fail_json(msg="refused: " + "; ".join(refused), **result)
    if not os.access(esptool, os.X_OK):
        module.fail_json(msg=f"no esptool at {esptool}", **result)
    try:
        with broker_client.Board(p["board"], path=p["socket"], timeout=p["wait"] + 600) as b:
            if module.check_mode:
                result["changed"] = True
                module.exit_json(why="Launcher is not on the chip: its image is written whole at 0x0 "
                                 "after an erase (the apply reads the table first)", **result)
            b.release(ttl=p["wait"] + 600)
            try:
                port, why = common.esp_port(devreg, p["board"], esptool, p["chip"])
                if why:
                    raise common.FlashError(why)
                mgr = common.read_manager(esptool, p["chip"], port)
                why = common.manager_refusal(mgr, p["app_manager_overwrite"], "writing at 0x0")
                if why:
                    raise common.FlashError("refused: " + why)
                base = [esptool, "--chip", p["chip"], "--port", port, "--baud", "921600"]
                rc, out = common.tool(base + ["--after", "no-reset", "erase-flash"], timeout=240)
                if rc != 0:
                    raise common.FlashError(f"esptool erase-flash failed (rc {rc}): " + out.strip()[-300:])
                rc, out = common.tool(base + ["write-flash", "--flash-mode", "keep", "--flash-freq", "keep",
                                              "--flash-size", "keep", "0x0", p["image"]], timeout=600)
                if rc != 0 or "Hash of data verified" not in out:
                    raise common.FlashError(f"esptool write-flash failed (rc {rc}): " + out.strip()[-300:])
            finally:
                b.reacquire()
            result["changed"] = True
            version, _how = console.enter(b, wait=p["wait"], entry=p["entry"])
            result["version"] = version
            if p["version"] and version != p["version"]:
                module.fail_json(msg=f"Launcher answers `{version}` after the write, the layout declares "
                                     f"{p['version']}", **result)
    except (broker_client.BrokerError, console.ConsoleError, common.FlashError) as e:
        module.fail_json(msg=str(e), **result)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
