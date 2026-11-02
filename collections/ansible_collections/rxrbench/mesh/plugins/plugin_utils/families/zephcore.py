"""The zephcore family: what it knows beyond its row (base.py lists the questions)."""
from ansible_collections.rxrbench.core.plugins.plugin_utils.families import base
from ansible_collections.rxrbench.mesh.plugins.module_utils import meshrf


class Family(base.Family):
    #: a UF2 board's bootloader is this plugin's to update (roles/board/tasks/bootloader_uf2.yml)
    bootloader_tasks = {"uf2": "bootloader_uf2"}
    #: its littlefs (prefs, identity, contacts; the key agreed with the firmware)
    config_regions = (("fs", "ZEPHCORE_FS"),)
    #: `zephcore state` names the family; its hardware builds print *float* for a number
    state = {"dialect": "meshcore", "version": 1, "names_family": True, "null_tokens": ["*float*"],
             "fields": {"target": "board", "version": "version", "build": "build", "role": "role"}}
    #: an image goes on a board only as the role the layout declares
    image_match = ("role",)
    #: its identity key, read and given back on its console; the admin password its layout
    #: declares
    secrets = ({"kind": "identity", "origin": "device", "via": "console"},
               {"kind": "admin_password", "origin": "declared"})
    #: the survey asks its state line (`zephcore state`) where its boards' consoles are; being
    #: a family with a state line it is asked before stock MeshCore, whose `ver` it also
    #: answers (asked after it, a zephcore board is taken for stock MeshCore)
    #: (on an nRF52 it is a Zephyr application, seen as zephyr-cdc, not a stock app's USB ids)
    survey = {"kinds": ["esp32-usb-jtag", "esp32-usb-cdc", "zephyr-cdc"],
              "keep": {"version": "version", "build": "build", "role": "role", "board": "board",
                       "pubkey_fp": "pubkey_fp"}}
    reboot = {"reboot": "reboot", "dialect": "meshcore", "expect_reply": False,
              "uptime": {"state_key": "uptime_s"}}

    def rf_declared(self, name, hostvars, managed):
        return meshrf.meshcore_declared(name, hostvars, managed)

    def survey_tags(self, probe, boards=None):
        if not (probe.get("version") or probe.get("build")):
            return set()
        return {"fw:zephcore", "radio:lora"} | ({f"role:{probe['role']}"} if probe.get("role") else set())

    def survey_describe(self, probe):
        if not (probe.get("version") or probe.get("build")):
            return ""
        return (f"zephcore {probe.get('role') or ''} {probe.get('version')} (build {probe.get('build')}) "
                f"on {probe.get('board')}, key {probe.get('pubkey_fp')}").replace("  ", " ")
