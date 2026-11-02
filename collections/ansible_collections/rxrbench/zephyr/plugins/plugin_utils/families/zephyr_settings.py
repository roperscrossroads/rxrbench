"""The zephyr_settings family: what it knows beyond its row (core's families/base.py lists the
questions).

A board's declaration is its `zephyr:` block:

    zephyr:
      device_key: app/key                 # a setting the device makes itself; escrowed
      settings: {app/psk: ..., tls/cert: ..., net/name: zb3}   # written through the shell
      secrets: {psk: app/psk, cert: tls/cert}  # which settings are secrets, by kind

A setting named under `secrets` or as the `device_key` is kept out of the broker's log and
of every task's output; the rest are plain.
"""
from ansible_collections.rxrbench.core.plugins.plugin_utils.families import base


class Family(base.Family):
    #: the settings live in the devicetree's storage partition: its offset and size come from
    #: the image's flash.json (`STORAGE`, `STORAGE_SIZE`), which staging reads from the build
    config_regions = (("storage", "STORAGE"),)

    #: device_key: made by the device at first boot, escrowed by `keys-export`, given back by
    #: `keys-restore`. psk and cert: the layout's values, compared by `secrets.yml`.
    secrets = ({"kind": "device_key", "origin": "device", "via": "console"},
               {"kind": "psk", "origin": "declared"},
               {"kind": "cert", "origin": "declared"})

    def board_secrets(self, hostvars):
        """Only the kinds this board declares a setting for."""
        z = hostvars.get("zephyr") or {}
        kinds = set((z.get("secrets") or {}).keys()) | ({"device_key"} if z.get("device_key") else set())
        return [dict(s) for s in self.secrets if s["kind"] in kinds]
