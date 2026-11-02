"""The Launcher plugin: ESP32 devices that run bmorcelli's Launcher, an app manager that lives
in its own partition and installs other firmware images beside it, one OTA slot each.

    launcher   known by Launcher's `version` on its serial console (the `launcher` dialect: no
               echo, newline-ended lines), brought to its declared state by this plugin's own
               update step (`roles/board/tasks/update_launcher.yml`): Launcher's version, the
               apps in its slots, the slot that boots, its settings and saved networks

core's plugin_utils/families says what a row's fields are. The WebUI client (`module_utils/
webui.py`, `tools/launcherctl.py`) is the second route, for a board with a registered wifi
address.
"""
from .families import launcher

FAMILIES = {
    "launcher": {"vocabulary": "launcher", "state_command": "version", "identity_key": None,
                 "build": "images", "config_via": "console", "radio": "none", "update": "launcher",
                 "console": "launcher", "locate": None, "plugin": "launcher"},
}

PLUGINS = {"launcher": launcher.Family()}

# A Launcher board is an ESP32 that esptool reads and writes whole like any other: its chip can
# be backed up, restored and erased (core's chip modules). It has no region level: Launcher's
# and its apps' settings are not regions an image's flash map names.
FLASHERS = {"launcher": ("chip",)}
