#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Declared settings written to a Zephyr board through its settings shell, and settings read."""
from __future__ import absolute_import, division, print_function

__metaclass__ = type  # pylint: disable=invalid-name

DOCUMENTATION = r"""
---
module: zephyr_settings
short_description: Settings of a Zephyr application, through its settings shell
description:
  - Reads each declared setting through the board's broker and writes the ones that differ
    (C(settings write string)); a setting named a secret never reaches the broker's log or this
    module's output. In check mode nothing is written and the differences are the plan.
  - The settings shell reads back at most 256 bytes of a value, so a longer declared value is
    refused before anything is written, and a secret's value would cross the console on a read.
    With O(digest_command) (a firmware command that prints a setting's SHA-256 and length) both
    are compared by digest instead.
  - Settings named in I(read) are returned by value (the task should keep them with C(no_log)).
options:
  board:
    description: The board's name (its broker's socket is named after it).
    type: str
    required: true
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  settings:
    description: The declared settings, name to value. A value is one word.
    type: dict
    default: {}
  secrets:
    description: The names of the settings whose values are secrets.
    type: list
    elements: str
    default: []
  digest_command:
    description: The firmware's own command that prints a setting's SHA-256 and length
      (C(zapp digest)), as the layout's C(zephyr.digest_command) names it. With it, secrets are
      compared without crossing the console and a value may be longer than the 256 bytes the
      settings shell reads back.
    type: str
  read:
    description: Settings to read and return (C(values)); a missing one is null.
    type: list
    elements: str
    default: []
  state_command:
    description: The application's command that prints rxrbench's state line (zapp's
      C(zapp state)), as the layout's C(zephyr.state_command) names it. Its line is read and
      checked (the checksum, the JSON) and returned as C(state). Only reads.
    type: str
  apply_allowed:
    description: The layout's go-ahead for writing (C(bench_apply_allowed)).
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: The declared settings
  rxrbench.zephyr.zephyr_settings:
    board: "{{ bench_board }}"
    socket: "{{ bench_socket | default(omit) }}"
    settings: "{{ zephyr.settings }}"
    secrets: "{{ zephyr.secrets.values() | list }}"
    apply_allowed: "{{ bench_apply_allowed | default(false) | bool }}"
  no_log: true
"""

RETURN = r"""
steps:
  description: "What differed, one line per setting, by name only (C(app/psk: set), C(net/name: set))."
  type: list
  returned: always
found:
  description: The settings asked for with I(read), name to value (null when the board has none).
  type: dict
  returned: always
state:
  description: The state line's JSON (C(v), C(family), C(fw), C(target), C(id) as zapp prints them).
  type: dict
  returned: when O(state_command) is given
"""

from ansible.module_utils.basic import AnsibleModule  # noqa: E402

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, gate  # noqa: E402
from ansible_collections.rxrbench.zephyr.plugins.module_utils import zsettings  # noqa: E402


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "socket": {"type": "path"},
            "settings": {"type": "dict", "default": {}, "no_log": True},
            "secrets": {"type": "list", "elements": "str", "default": []},
            "read": {"type": "list", "elements": "str", "default": []},
            "digest_command": {"type": "str"},
            "state_command": {"type": "str"},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    secret = set(p["secrets"])
    steps, found, state = [], {}, None
    # A declared value the shell could never read back whole is refused before the board is
    # asked anything, in a plan too.
    long = [why for name, want in p["settings"].items()
            if (why := zsettings.too_long(name, "" if want is None else str(want),
                                          p["digest_command"]))]
    if long:
        module.fail_json(msg="; ".join(long), steps=[])
    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            for name, want in p["settings"].items():
                want = "" if want is None else str(want)
                if not zsettings.differs(b, name, want, secret=want if name in secret else None,
                                         digest_command=p["digest_command"]):
                    continue
                steps.append(f"{name}: set")
                if module.check_mode:
                    continue
                gate.check(module)   # the layout's go-ahead, before the first write
                zsettings.write(b, name, want, secret=name in secret,
                                digest_command=p["digest_command"])
            for name in p["read"]:
                found[name] = zsettings.read(b, name, secret=p["settings"].get(name) if name in secret else None)
            if p["state_command"]:
                state = zsettings.state(b, p["state_command"])
    except (broker_client.BrokerError, zsettings.SettingsError) as e:
        module.fail_json(msg=str(e), steps=steps)
    result = {"changed": bool(steps), "steps": steps, "found": found}
    if state is not None:
        result["state"] = state
    module.exit_json(**result)


if __name__ == "__main__":
    main()
