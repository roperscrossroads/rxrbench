"""The meshcore family: what it knows beyond its row (base.py lists the questions)."""
from ansible_collections.rxrbench.core.plugins.plugin_utils.families import base
from ansible_collections.rxrbench.mesh.plugins.module_utils import meshrf


class Family(base.Family):
    #: an ESP32 release's config regions: the file system its prefs and identity key live in, and the
    #: NVS (Bluetooth bonds), from the merged image's own partition table; on an nRF52 or an
    #: RP2040 its UF2 names no regions, so only the whole-image ESP32 boards have this level
    config_regions = (("fs", "SPIFFS"), ("nvs", "NVS"))
    region_flashers = ("esp-image",)
    #: a UF2 board's bootloader is this plugin's to update (roles/board/tasks/bootloader_uf2.yml)
    bootloader_tasks = {"uf2": "bootloader_uf2"}
    #: its identity key, read (`get prv.key`, serial console only) and given back (`set prv.key`)
    #: on its console; its admin password cannot be read, so the tool never holds it
    secrets = ({"kind": "identity", "origin": "device", "via": "console"},)
    #: what it runs, for the flashers: no state line, so its `ver` answer, "v1.17.1-d929643
    #: (Build: 14-Aug-2026)": the release, and the commit it was built from (the build id a
    #: catalogued release image carries)
    identity_probe = {"command": "ver", "dialect": "meshcore", "lineless": True,
                      "fields": {"version": "version", "build": "build", "role": "role"},
                      # the survey asks its role too (repeater, room server: the image's kind)
                      "also": [{"command": "get role", "field": "role",
                                "pattern": r"^\s*(?:-> )?> (\S+)"}],
                      "fallback": {"command": "ver", "require": "(Build:",
                                   # through the broker the answer; on the port the
                                   # console's line, "  -> v1.17.1-..."
                                   "fields": {"version": r"^\s*(?:-> )?(v\d[\w.]*)",
                                              "build": r"^\s*(?:-> )?v\d[\w.]*-([0-9a-f]{7,40})\b"},
                                   "optional": ["build"]}}
    #: the survey asks a board's text console for `ver` (then its role) when no family with a
    #: state line answered: a repeater or room server. A companion build speaks MeshCore's
    #: binary frames on USB instead: asked by a probe of its own (a device query), on a stock
    #: app or an ESP32 nothing else answered.
    survey = {"kinds": ["heltec-t114", "xiao-nrf52840", "esp32-usb-jtag", "esp32-usb-cdc",
                        "uart-bridge"],
              "keep": {"version": "version", "build": "build", "role": "role"},
              "tool": "probe_meshcore_companion.py", "modes": ["stock-app"],
              "unanswered": ["esp32-usb-jtag", "esp32-usb-cdc"]}
    #: its console reboots it, but it has no state line to say how long it has been up; an
    #: ESP32's console stays open across the reset and the node answers again at once, so the
    #: reboot is told by what it prints as it boots: the ROM's reset line, or the firmware's ID
    reboot = {"reboot": "reboot", "dialect": "meshcore", "expect_reply": False, "uptime": None,
              "banner": r"^rst:0x[0-9a-f]+ \(|^(Repeater|Room) ID: [0-9A-F]{16}", "ready": "ver"}

    def rf_declared(self, name, hostvars, managed):
        return meshrf.meshcore_declared(name, hostvars, managed)

    def survey_tags(self, probe, boards=None):
        if not probe.get("version"):
            return set()
        return {"fw:meshcore", "radio:lora"} | ({f"role:{probe['role']}"} if probe.get("role") else set())

    def enroll_note(self, probe):
        if not probe.get("version"):
            return {}
        build = f"-{probe['build']}" if probe.get("build") else ""
        return {"delivered": f"stock MeshCore {probe['version']}{build}"}

    def survey_describe(self, probe):
        if not probe.get("version"):
            return ""
        build = f" ({probe['build']})" if probe.get("build") else ""
        if probe.get("role") == "companion":
            return (f"stock MeshCore companion {probe['version']}{build} on a {probe.get('model')}, "
                    "answering its USB frames")
        role = f" {probe['role']}" if probe.get("role") else ""
        return f"stock MeshCore{role} {probe['version']}{build}, answering its text console"
