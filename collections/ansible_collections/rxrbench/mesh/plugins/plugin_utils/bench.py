"""The mesh plugin: four families of LoRa mesh firmware, in three configuration vocabularies.

    zephyrtastic  a Zephyr port of Meshtastic: built here from its catalog of classes
    zephcore      MeshCore-compatible firmware on Zephyr
    meshtastic    stock Meshtastic
    meshcore      stock MeshCore

The `meshtastic` vocabulary is Meshtastic's schema (written through the phone API carried
over a shell, `shell-tunnel`); `meshtastic_stock` is the same schema written to stock firmware
through its own phone API, on its USB port or its network API (`phone-api`); the `meshcore`
vocabulary is MeshCore's CLI prefs (`cli`). Each names its task files in this collection's
`board` role. core's plugin_utils/families says
what a row's fields are.
"""
from .families import meshcore, meshtastic, zephcore, zephyrtastic

FAMILIES = {
    "zephyrtastic": {"vocabulary": "meshtastic", "state_command": "meshtastic state",
                     "identity_key": "class", "build": "catalog", "config_via": "shell-tunnel",
                     "radio": "managed", "console": "zephyr-shell", "locate": "shell", "plugin": "zephyrtastic"},
    # Its own text CLI, `zephcore state`, a role. Its images are listed until it has a recipe.
    "zephcore": {"vocabulary": "meshcore", "state_command": "zephcore state",
                 "identity_key": "role", "build": "images", "config_via": "cli",
                 "radio": "managed", "console": "meshcore", "locate": None, "plugin": "zephcore"},
    # The same CLI and console, no state line; flashed by its own tools.
    "meshcore": {"vocabulary": "meshcore", "state_command": None, "identity_key": None,
                 "build": "images", "config_via": "cli", "radio": "unmanaged",
                 "console": "meshcore", "locate": None, "plugin": "meshcore"},
    # Stock Meshtastic: no console this tool speaks (its USB port is the phone API's binary
    # framing), so no broker; its config is read and written through that phone API by the
    # stock library (meshtastic_stock_config), on the port or the network, in the same
    # `meshtastic:` host_vars a zephyrtastic board takes. Its radio is written when declared and
    # left at the firmware's defaults when not (`unmanaged`: a declaration is not required).
    "meshtastic": {"vocabulary": "meshtastic_stock", "state_command": None, "identity_key": None,
                   "build": "images", "config_via": "phone-api", "radio": "unmanaged",
                   "console": None, "locate": None, "plugin": "meshtastic"},
}

# The USB ids of the boards these families run on (core's hwsurvey.KINDS says what a row is):
# how the survey tells a board in its bootloader from the same board running an app.
USB_KINDS = {
    "2886:0045": {"kind": "xiao-nrf52840", "mcu": "nrf52840", "mode": "bootloader",
                  "serial_is": "chip_id",
                  "note": "Adafruit UF2 bootloader (Seeed's 'Sense' label is a mislabel); the "
                          "only mode that takes UF2/DFU"},
    "239a:810b": {"kind": "xiao-nrf52840", "mcu": "nrf52840", "mode": "stock-app",
                  "serial_is": "chip_id",
                  "note": "the factory app of a XIAO radio kit ('-BOOT' is in its product "
                          "string; it is not a bootloader)"},
    "239a:8029": {"kind": "heltec-t114", "mcu": "nrf52840", "mode": "stock-app",
                  "serial_is": "chip_id",
                  "note": "a Heltec T114 (HT-n5262) running an app built on the Adafruit nRF52 "
                          "core, which gives the FICR id as the USB serial"},
    "239a:0071": {"kind": "heltec-t114", "mcu": "nrf52840", "mode": "bootloader",
                  "serial_is": "chip_id",
                  "note": "a Heltec T114's UF2 bootloader, by the ids in the bootloader's "
                          "source (the same product string as its stock apps)"},
}

PLUGINS = {m.__name__.rsplit(".", 1)[-1]: m.Family() for m in
           (meshcore, meshtastic, zephcore, zephyrtastic)}
