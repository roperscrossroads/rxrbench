"""The meshtastic family: what it knows beyond its row (base.py lists the questions)."""
import json
import posixpath

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
    #: its identity key, read over its own phone API on its serial port; never written
    secrets = ({"kind": "identity", "origin": "device", "via": "serial"},)
    #: stock firmware has no state line: it is asked over its own phone API by a tool this
    #: plugin deploys, when a board says it runs it (a XIAO does, in its descriptor) or an
    #: ESP32 board answered as nothing else
    survey = {"tool": "probe_meshtastic.py", "modes": ["stock-app"],
              "unanswered": ["esp32-usb-jtag", "esp32-usb-cdc"]}
    #: what a board runs, for the flashers: the same probe. A release's version ends in its
    #: commit (2.7.26.54e0d8d), which is the build id an image staged from it carries.
    identity_probe = {"tool": "probe_meshtastic.py", "fields": {"version": "firmware_version"},
                      "build_from": {"key": "firmware_version", "pattern": r"\.([0-9a-f]{7,40})$"},
                      "timeout": 60}
    #: scenario steps on a stock node, which has no console: through its phone API
    #: (roles/board/tasks/step_meshtastic_stock.yml, module meshtastic_stock_step)
    step_tasks = "step_meshtastic_stock"
    #: a running stock app is asked for its bootloader's UF2 mode by the stock CLI
    uf2_enter = {"app_tools": ["~/.venv-mesh/bin/meshtastic --port {port} --enter-dfu",
                               "~/.local/bin/meshtastic --port {port} --enter-dfu"]}

    #: the release map's names for the two app slots, and the subtype that finds each in a
    #: table that names them otherwise (the SenseCAP Indicator's `flashApp` is ota_1)
    APP_SUBTYPES = {"app0": "ota_0", "app1": "ota_1"}

    def release_partitions(self, member, read):
        """A release's `<target>.mt.json` beside its `<target>.factory.bin` is the map its web
        flasher writes a board from: each file with a `part_name` goes to that partition. The
        factory file holds app0 already; the rest (the OTA loader in app1, the file system in
        spiffs) is what a full install adds. A release without a map adds nothing."""
        if not member.endswith(".factory.bin"):
            return []
        raw = read(member[:-len(".factory.bin")] + ".mt.json")
        if raw is None:
            return []
        mt = json.loads(raw)
        labels = {p.get("name") for p in mt.get("part") or []}
        out = []
        for f in mt.get("files") or []:
            part = f.get("part_name")
            if not part or part == "app0":
                continue
            out.append({"partition": part if part in labels else self.APP_SUBTYPES.get(part, part),
                        "member": posixpath.join(posixpath.dirname(member), f["name"]),
                        **({"md5": f["md5"]} if f.get("md5") else {})})
        return out

    def rf_declared(self, name, hostvars, managed):
        return meshrf.meshtastic_declared(name, hostvars, managed)

    def rf_check(self, name, hostvars, rules):
        """The width of the preset it transmits on must fit the band."""
        return meshrf.meshtastic_rf_check(name, hostvars, rules)

    def survey_tags(self, probe, boards=None):
        """What stock Meshtastic says of itself over its phone API."""
        t = {"fw:stock-meshtastic", "radio:lora"}
        if probe.get("hw_model"):
            t.add(f"hw:{probe['hw_model']}")
        if probe.get("has_bluetooth"):
            t.add("radio:ble")
        if probe.get("has_wifi"):
            t.add("radio:wifi")
        return t

    def enroll_note(self, probe):
        said = {k: v for k, v in (("node_id", probe.get("node_id")),
                                  ("long_name", probe.get("long_name")),
                                  ("stock_fw", probe.get("firmware_version")),
                                  ("hw_model", probe.get("hw_model")),
                                  ("ble_mac", probe.get("ble_mac"))) if v}
        return {**({"model": probe["hw_model"]} if probe.get("hw_model") else {}),
                **({"delivered": f"stock Meshtastic {probe['firmware_version']}"}
                   if probe.get("firmware_version") else {}),
                **({"alias": probe["short_name"]} if probe.get("short_name") else {}),
                **({"identity": said} if said else {})}

    def survey_describe(self, probe):
        if not probe.get("firmware_version"):
            return ""
        return (f"stock Meshtastic {probe['firmware_version']} ({probe.get('hw_model')}), "
                f"'{probe.get('long_name')}' node {probe.get('node_id')}, region {probe.get('region')}")
