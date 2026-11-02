"""The linux family: a Linux device reached over ssh, with no update mechanism this tool
drives. What it runs is read; changing it is re-imaging (a card). The two families that
update subclass this one. base.py lists the questions."""
from ansible_collections.rxrbench.core.plugins.plugin_utils.families import base


class Family(base.Family):
    #: what `just diag` asks over ssh, unless the layout says: nothing here assumes an init
    #: system or a logger
    diag_commands = ({"name": "os-release", "cmd": "cat /etc/os-release"},
                     {"name": "kernel", "cmd": "uname -a"},
                     {"name": "uptime", "cmd": "uptime"},
                     {"name": "disk", "cmd": "df -k"},
                     {"name": "kernel-log", "cmd": "dmesg | tail -n 200"})
    #: rebooted over ssh, and proven by its uptime
    reboot = {"reboot": "sync; (sleep 1; reboot) >/dev/null 2>&1 &",
              "uptime": {"command": "cat /proc/uptime", "pattern": r"^(\d+)"}}
    #: its ssh host key: made by the device at first boot, escrowed by the site, given back
    #: to a re-imaged device so it stays the host its peers know
    secrets = ({"kind": "host_key", "origin": "device", "via": "ssh"},)

    #: The unit that gates a slot as healthy and how long it soaks, or None: {unit, soak_s}.
    health = None
    #: Where an update lands on the device before it is installed, or None when the family
    #: cannot say (a layout must: bench_rauc_remote_bundle).
    remote_bundle = None
    #: Where the ssh host key is on the device: the first of these that is there (which type
    #: of key an image's ssh server makes is the image's choice).
    host_key = ("/etc/ssh/ssh_host_ed25519_key", "/etc/ssh/ssh_host_ecdsa_key")
    #: The LED `just locate` blinks, under /sys/class/leds.
    led = "ACT"

    def board_facts(self, hostvars, groups):
        """What the task files read for this device: the family's answers, each overridden by
        the layout's own word for it."""
        health = self.health or {}
        return {
            "health_unit": hostvars.get("bench_rauc_health_unit") or health.get("unit"),
            "health_soak": hostvars.get("bench_health_soak") or health.get("soak_s") or 0,
            "remote_bundle": hostvars.get("bench_rauc_remote_bundle") or self.remote_bundle,
            "host_key_paths": ([hostvars["bench_host_key_path"]] if hostvars.get("bench_host_key_path")
                               else list(self.host_key)),
            "led_path": hostvars.get("bench_led_path")
            or f"/sys/class/leds/{hostvars.get('bench_led_name') or self.led}",
        }
