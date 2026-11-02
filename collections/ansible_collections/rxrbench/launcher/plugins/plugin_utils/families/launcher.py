"""The launcher family: an ESP32 that runs Launcher, the app manager, in its own partition and
the images Launcher installed in OTA slots beside it (core's families/base.py lists the
questions this answers).

A board's declaration is its `firmware:` block (the Launcher image for its board type, from
the firmware catalog, `kind: manager`) and its `launcher:` block:

    launcher:
      apps: {camstr: {image: cam-stream}}   # slot label (six characters, what Launcher makes
                                            # of the app's name) -> a catalog image of kind `app`
      boot: camstr                          # the slot the otadata selects
      boot_to_app: true                     # Launcher's own settings this tool keeps
      boot_timer: 4
      wui_user: admin                       # the WebUI user; the password is a declared secret
      wifi: [{ssid: lab-iot}]               # saved networks; each password a declared secret
      prune: false                          # slots not declared are left alone

Launcher has no state line; its identity is read by its `version` command (the fallback of
the identity spec) in the `launcher` console dialect, which echoes nothing. The family's
images are told apart by their version alone: an installed app's bytes cannot be read back
through Launcher, so an app is known by its slot's label and size and by the install record
the tool keeps in the board's facts.
"""
from ansible_collections.rxrbench.core.plugins.plugin_utils.families import base


def _slot_label(name):
    """`images.slot_label`, imported late: the families package stays import-light."""
    from ...module_utils import images  # pylint: disable=import-outside-toplevel
    return images.slot_label(name)

IMAGE_KINDS = ("manager", "app")
#: A slot label is what Launcher makes of an app's name: six lowercase letters and digits
#: (`images.slot_label`: `buddy` -> `buddy0`, `cam-stream` -> `camstr`). A layout declares the
#: label the slot has, or will get when the install names it so.
LABEL_LEN = 6


class Family(base.Family):
    state = {"dialect": "launcher", "fields": {"version": "version"},
             "fallback": {"command": "version", "require": "Launcher ",
                          "fields": {"version": r"^Launcher (\S+)"}}}
    #: `reboot` boots the selected app (a software reset follows the otadata); Launcher does
    #: not say how long it has been up.
    reboot = {"reboot": "reboot", "dialect": "launcher", "expect_reply": True, "uptime": None}
    #: The site is the authority for both: the WebUI password and each saved network's.
    secrets = ({"kind": "wui_password", "origin": "declared"},
               {"kind": "wifi_psk", "origin": "declared"})
    #: The survey asks every ESP32 face for `version`; `fw:launcher` and the version are kept.
    survey = {"kinds": ["esp32-usb-jtag", "esp32-usb-cdc", "uart-bridge"],
              "keep": {"version": "version"}}
    diag_commands = ({"name": "version", "cmd": "version"}, {"name": "partitions", "cmd": "partitions"})

    def image(self, fw, catalog):
        record, problems = super().image(fw, catalog)
        img = catalog["images"].get(fw.get("image")) or {}
        if record is not None and img.get("kind", "manager") != "manager":
            problems.append(f"image {fw['image']} is an app Launcher installs, not a Launcher image: "
                            "declare it under launcher.apps")
        return record, problems

    def board_secrets(self, hostvars):
        block = hostvars.get("launcher") or {}
        kinds = {"wui_password"} | ({"wifi_psk"} if block.get("wifi") else set())
        return [dict(s) for s in self.secrets if s["kind"] in kinds]

    def board_facts(self, hostvars, groups):
        block = hostvars.get("launcher") or {}
        return {"launcher_apps": block.get("apps") or {}, "launcher_boot": block.get("boot"),
                "launcher_prune": bool(block.get("prune", False))}

    def check_layout(self, all_hostvars):
        out = []
        for name, hv in all_hostvars.items():
            if (hv.get("firmware") or {}).get("family") != "launcher":
                continue
            block = hv.get("launcher") or {}
            apps = block.get("apps") or {}
            for label, app in apps.items():
                if len(label) != LABEL_LEN or not (label.isalnum() and label.isascii() and label == label.lower()):
                    made = _slot_label(label)
                    out.append(f"{name}: launcher.apps.{label}: a slot label is {LABEL_LEN} lowercase letters "
                               f"and digits, what Launcher makes of an app's name (of {label!r}: {made!r})")
                if not isinstance(app, dict) or not app.get("image"):
                    out.append(f"{name}: launcher.apps.{label} names no image")
            boot = block.get("boot")
            if boot and boot not in apps:
                out.append(f"{name}: launcher.boot is {boot!r}, which launcher.apps does not declare")
            for i, net in enumerate(block.get("wifi") or []):
                if not isinstance(net, dict) or not net.get("ssid"):
                    out.append(f"{name}: launcher.wifi[{i}] names no ssid")
        return out

    def chip_regions(self, hostvars, partitions):
        """A Launcher layout declares the manager and the apps under `launcher.apps`: Launcher's
        own slot and those apps' slots are its, and the shared data too when every image on the
        chip is one of them. An app it does not declare stays other firmware (`prune: false`
        leaves it, and the plan says it is there); the data it shares is then not the layout's
        alone. The manager is core's structural finding (`esppart.manager`), not a size here."""
        partitions = partitions or {}
        mgr = partitions.get("manager")
        if not mgr:
            return {"owned": [], "describe": ""}
        inv = partitions.get("inventory") or []
        declared = set((hostvars.get("launcher") or {}).get("apps") or {})
        owned = [mgr["label"]] + [a["label"] for a in mgr.get("apps") or [] if a["label"] in declared]
        images = [r for r in inv if r.get("kind") == "app" and r.get("recogniser")]
        if all(r["label"] in owned or r.get("owner") == "declared" for r in images):
            owned += [r["label"] for r in inv if r.get("kind") == "data"]
        installed = [r["label"] for r in images if r["label"] != mgr["label"]]
        return {"owned": owned,
                "describe": f"Launcher in {mgr['label']} at {mgr['offset']:#x} with apps: "
                            f"{', '.join(installed) or 'none installed'}"}

    def survey_tags(self, probe, boards=None):
        tags = {"fw:launcher"}
        if probe.get("version"):
            tags.add(f"launcher:{probe['version']}")
        return tags

    def survey_describe(self, probe):
        return f"Launcher {probe.get('version') or '?'}"

    def enroll_note(self, probe):
        return {"delivered": f"Launcher {probe.get('version')}"} if probe.get("version") else {}
