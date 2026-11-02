#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Declared files written to a NuttX board through NSH, its storage mounted first; files read."""
from __future__ import absolute_import, division, print_function

__metaclass__ = type  # pylint: disable=invalid-name

DOCUMENTATION = r"""
---
module: nuttx_files
short_description: Files a NuttX board keeps, through NSH
description:
  - Mounts the board's storage where it declares it (unless mounted), reads each declared file
    through the board's broker and writes the ones that differ (C(echo value > path)); a file
    named a secret never reaches the broker's log or this module's output. In check mode
    nothing is written and the differences are the plan.
  - Files named in I(read) are returned by content (the task should keep them with C(no_log)).
options:
  board:
    description: The board's name (its broker's socket is named after it).
    type: str
    required: true
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  storage:
    description: "The storage to mount first: C(at), C(type) (C(hostfs) on the sim), C(source)."
    type: dict
  files:
    description: The declared files, path to content. A content is one word.
    type: dict
    default: {}
  secrets:
    description: The paths of the files whose contents are secrets.
    type: list
    elements: str
    default: []
  read:
    description: Files to read and return (C(found)); a missing one is null.
    type: list
    elements: str
    default: []
  apply_allowed:
    description: The layout's go-ahead for writing (C(bench_apply_allowed)).
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: The declared files
  rxrbench.nuttx.nuttx_files:
    board: "{{ bench_board }}"
    storage: "{{ nuttx.storage }}"
    files: "{{ nuttx.files }}"
    secrets: "{{ nuttx.secrets.values() | list }}"
    apply_allowed: "{{ bench_apply_allowed | default(false) | bool }}"
  no_log: true
"""

RETURN = r"""
steps:
  description: "What differed, one line per file, by path only (C(/data/psk: set)), and C(mounted /data) when the storage was."
  type: list
  returned: always
found:
  description: The files asked for with I(read), path to content (null when the board has none).
  type: dict
  returned: always
"""

from ansible.module_utils.basic import AnsibleModule  # noqa: E402

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, gate  # noqa: E402
from ansible_collections.rxrbench.nuttx.plugins.module_utils import nfiles  # noqa: E402


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "socket": {"type": "path"},
            "storage": {"type": "dict"},
            "files": {"type": "dict", "default": {}, "no_log": True},
            "secrets": {"type": "list", "elements": "str", "default": []},
            "read": {"type": "list", "elements": "str", "default": []},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    secret = set(p["secrets"])
    steps, found = [], {}
    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            if p["storage"]:
                # mounting changes nothing the layout declares: done in a plan too, so the
                # files can be read
                if nfiles.mount(b, p["storage"]):
                    steps.append(f"mounted {p['storage']['at']}")
            for path, want in p["files"].items():
                want = "" if want is None else str(want)
                if nfiles.read(b, path, secret=want if path in secret else None) == want:
                    continue
                steps.append(f"{path}: set")
                if module.check_mode:
                    continue
                gate.check(module)
                nfiles.write(b, path, want, secret=path in secret)
            for path in p["read"]:
                found[path] = nfiles.read(b, path, secret=p["files"].get(path) if path in secret else None)
    except (broker_client.BrokerError, nfiles.FilesError) as e:
        module.fail_json(msg=str(e), steps=steps)
    module.exit_json(changed=any(s.endswith(": set") for s in steps), steps=steps, found=found)


if __name__ == "__main__":
    main()
