"""The Zephyr plugin: any Zephyr application that keeps its configuration in Zephyr's settings
subsystem and takes the settings shell (`CONFIG_SETTINGS_SHELL`).

    zephyr_settings  known by the bytes of its image (no state line), spoken to through the
                     Zephyr shell; its declared settings are written one by one through
                     `settings write`, and its secrets are settings too: one the device makes
                     (escrowed by the site) and ones the layout declares

core's plugin_utils/families says what a row's fields are. The plugin's example application
(`sim/zapp`, docs/SIMULATED.md) is one such application; it is not the only one.
"""
from .families import zephyr_settings

FAMILIES = {
    "zephyr_settings": {"vocabulary": "settings", "config_via": "console", "console": "zephyr-shell",
                        "plugin": "zephyr_settings"},
}

PLUGINS = {"zephyr_settings": zephyr_settings.Family()}
