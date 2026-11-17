#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Declared files on a Linux device, over ssh."""
from __future__ import absolute_import, division, print_function

__metaclass__ = type  # pylint: disable=invalid-name

DOCUMENTATION = r"""
---
module: linux_files
short_description: Small text files a layout declares for a Linux device
description:
  - Compares each declared file with what the device holds, by SHA-256 and mode, and writes
    the ones that differ, whole. A file's content never crosses the connection towards the
    bench and never reaches this module's output, so it may hold a secret.
  - For files that are the device's own to keep across updates (a client's config on its data
    partition). A file an image carries belongs in the image.
  - Needs a shell, C(sha256sum) and C(cat) on the device, nothing else. In check mode nothing
    is written and the differences are the plan.
options:
  route:
    description: The device's ssh route, as the inventory resolves it (C(bench_routes.shell)).
    type: dict
    required: true
  files:
    description: The declared files, path to C({content, mode}). C(mode) is octal, as a
      string; the default is C("600").
    type: dict
    default: {}
  apply_allowed:
    description: The layout's go-ahead for writing (C(bench_apply_allowed)).
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: The declared files
  rxrbench.linux.linux_files:
    route: "{{ bench_routes.shell }}"
    files:
      /data/app/config: {content: "NAME=node-a\n", mode: "600"}
    apply_allowed: "{{ bench_apply_allowed | default(false) | bool }}"
"""

RETURN = r"""
steps:
  description: "What differed, one line per file, by path only (C(/data/app/config: missing))."
  type: list
  returned: always
"""

from ansible.module_utils.basic import AnsibleModule  # noqa: E402

from ansible_collections.rxrbench.core.plugins.module_utils import gate, ssh  # noqa: E402
from ansible_collections.rxrbench.linux.plugins.module_utils import files  # noqa: E402


def main():
    module = AnsibleModule(
        argument_spec={
            "route": {"type": "dict", "required": True},
            "files": {"type": "dict", "default": {}, "no_log": True},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before the device is touched
    steps = []
    for path, want in p["files"].items():
        if not isinstance(want, dict) or not isinstance(want.get("content"), str):
            module.fail_json(msg=f"{path}: a declared file is {{content, mode}}", steps=[])
        if not path.startswith("/"):
            module.fail_json(msg=f"{path}: a declared file is named by its whole path", steps=[])
    try:
        target = ssh.Target(p["route"])
        for path, want in sorted(p["files"].items()):
            mode = str(want.get("mode") or "600")
            why = files.differs(target, path, want["content"], mode)
            if why is None:
                continue
            steps.append(f"{path}: {why}")
            if not module.check_mode:
                files.write(target, path, want["content"], mode)
    except ssh.SshError as e:
        module.fail_json(msg=str(e), steps=steps)
    module.exit_json(changed=bool(steps), steps=steps)


if __name__ == "__main__":
    main()
