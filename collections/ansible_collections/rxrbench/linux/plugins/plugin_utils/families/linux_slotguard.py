"""The linux_slotguard family: Linux with RAUC A/B slots and slotguard's health gating.

A slot is not good because it booted. A unit soaks, checks the system and only then lets the
slot be marked good; a slot that never passes is failed over by the bootloader's attempt
counter. So "updated" has a second stage after the reboot, and this family says which unit
answers for it and how long it takes. Everything else is linux_rauc."""
from . import linux_rauc


class Family(linux_rauc.Family):
    names = ("linux_slotguard",)
    #: boot-health-check soaks before the slot is marked good
    health = {"unit": "boot-health-check.service", "soak_s": 300}
    #: slotguard mounts a data partition; a bundle lands there
    remote_bundle = "/data/update.raucb"
    #: the host keys persist on the data partition, across both slots
    host_key = ("/data/ssh/ssh_host_ed25519_key", "/data/ssh/ssh_host_ecdsa_key")
    diag_commands = (*linux_rauc.Family.diag_commands,
                     {"name": "boot-request", "cmd": "boot-request status"},
                     {"name": "failed-units", "cmd": "systemctl --failed --no-pager"},
                     {"name": "health", "cmd": "journalctl -b -u boot-health-check -u boot-slot-health "
                                               "--no-pager -n 200", "timeout": 40},
                     {"name": "journal", "cmd": "journalctl -b -n 300 --no-pager", "timeout": 40})
