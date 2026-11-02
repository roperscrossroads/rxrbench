#!/usr/bin/python
"""Read an ESP32 running Launcher, through its WebUI."""

DOCUMENTATION = r"""
module: launcher_facts
short_description: What an ESP32 under Launcher holds, read through Launcher's WebUI
description:
  - Reads Launcher's version, the SD card's usage, the installed apps, the app slots (label,
    offset, size, whether it is the one running, the data partitions it uses), the free flash
    and, on builds that serve it, the NVS namespaces with their key counts (never a value).
  - Launcher only serves its WebUI while it runs. A device that does not answer is reported
    with C(launcher.running=false), not as a failure, unless O(require) is set; it may be
    running one of its apps.
  - Changes nothing on the device's flash and behaves the same in check mode. A login does
    replace the WebUI's one session (a browser open on it is logged out) and stores its token
    in the device's NVS; O(session) reuses an earlier one.
options:
  url:
    description: The WebUI, C(http://host[:port]).
    type: str
    required: true
  username:
    description: The WebUI user.
    type: str
    default: admin
  password:
    description: The WebUI password. Without it only the parts that need no login are read
      (version, SD usage, installed apps).
    type: str
  session:
    description: A session token from an earlier login, tried first.
    type: str
  require:
    description: Fail when no Launcher answers.
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Read the device while it is in Launcher
  rxrbench.launcher.launcher_facts:
    url: http://192.0.2.40
    password: "{{ launcher_password }}"
  register: launcher
"""

RETURN = r"""
launcher:
  description: C(running) (a Launcher answered), C(version), C(logged_in).
  returned: always
  type: dict
sd:
  description: The SD card's C(totalBytes), C(usedBytes), C(freeBytes).
  returned: when Launcher answers
  type: dict
apps:
  description: The installed apps, C(label) and display C(name).
  returned: when Launcher answers
  type: list
slots:
  description: The app slots (C(label), C(app), C(subtype), C(offset), C(size), C(running),
    C(data)); Launcher's own partition is left out.
  returned: when logged in
  type: list
flash:
  description: C(size), C(free) (bytes in unallocated ranges), C(dirty) (the partition
    manager holds unapplied edits).
  returned: when logged in
  type: dict
nvs:
  description: Key counts per NVS namespace, or null on a build without the NVS page.
  returned: when logged in
  type: dict
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.launcher.plugins.module_utils import webui


def main():
    module = AnsibleModule(argument_spec={
        "url": {"type": "str", "required": True},
        "username": {"type": "str", "default": "admin"},
        "password": {"type": "str", "no_log": True},
        "session": {"type": "str", "no_log": True},
        "require": {"type": "bool", "default": False},
    }, supports_check_mode=True)
    p = module.params
    try:
        ui = webui.WebUI(p["url"], p["username"], p["password"], p["session"])
        facts = webui.facts(ui)
    except webui.LauncherError as e:
        module.fail_json(msg=str(e))
    if p["require"] and not facts["launcher"]["running"]:
        module.fail_json(msg=f"no Launcher WebUI answers at {p['url']}", **facts)
    module.exit_json(changed=False, **facts)


if __name__ == "__main__":
    main()
