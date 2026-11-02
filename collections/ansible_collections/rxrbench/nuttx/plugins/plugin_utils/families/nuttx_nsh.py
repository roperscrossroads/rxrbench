"""The nuttx_nsh family: what it knows beyond its row (core's families/base.py lists the
questions).

A board's declaration is its `nuttx:` block:

    nuttx:
      storage: {at: /data, type: hostfs, source: /some/host/dir}   # mounted first (sim: hostfs)
      files: {/data/psk: ..., /data/cert: ..., /data/name: nb1}    # written through NSH
      secrets: {psk: /data/psk, cert: /data/cert}                  # which files are secrets
      facts: [/data/name]                                          # what `just facts` reads
"""
from ansible_collections.rxrbench.core.plugins.plugin_utils.families import base


class Family(base.Family):
    #: NSH prints no state line; what the board is comes from `uname -a`, read by pattern:
    #: the release, the build (its commit and time: two builds of one commit differ by the
    #: time, which the staging tool records the same way), the machine.
    state = {"fallback": {"command": "uname -a", "require": "NuttX",
                          "fields": {"version": r"^NuttX (\S+) ",
                                     "build": r"^NuttX \S+ ([0-9a-f]+ \w+ +\d+ \d+ [\d:]+) ",
                                     "target": r"^NuttX \S+ [0-9a-f]+ \w+ +\d+ \d+ [\d:]+ (\S+) "}}}
    #: a power-off; the bench starts the board again (a real board's power is its own)
    reboot = {"reboot": "poweroff", "expect_reply": False}
    secrets = ({"kind": "psk", "origin": "declared"}, {"kind": "cert", "origin": "declared"})

    def board_secrets(self, hostvars):
        """Only the kinds this board declares a file for."""
        kinds = set(((hostvars.get("nuttx") or {}).get("secrets") or {}).keys())
        return [dict(s) for s in self.secrets if s["kind"] in kinds]
