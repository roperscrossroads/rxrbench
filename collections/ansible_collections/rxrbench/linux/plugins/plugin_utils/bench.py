"""The Linux plugin: embedded Linux devices reached over ssh, in three tiers.

    linux            a Linux device: what it runs is read; changing it is re-imaging
    linux_rauc       with RAUC A/B slots: a signed bundle over ssh
    linux_slotguard  with RAUC and slotguard's health gate: a slot is good after its soak

A device of these families has no console this tool reads and no radio it sets. Its image
may have no Python: every step drives ssh from the host that can reach the device. core's
plugin_utils/families says what a row's fields are.
"""
from .families import linux, linux_rauc, linux_slotguard

_ROW = {"vocabulary": "linux", "locate": "led"}
FAMILIES = {
    "linux": {**_ROW, "update": "linux", "plugin": "linux"},
    "linux_rauc": {**_ROW, "update": "rauc", "plugin": "linux_rauc"},
    "linux_slotguard": {**_ROW, "update": "rauc", "plugin": "linux_slotguard"},
}

PLUGINS = {m.__name__.rsplit(".", 1)[-1]: m.Family() for m in (linux, linux_rauc, linux_slotguard)}

# An A/B device's image is a RAUC bundle, installed by the family's own update step: nothing
# beside writing it (no config regions, no whole chip to read back).
FLASHERS = {"rauc": ()}
