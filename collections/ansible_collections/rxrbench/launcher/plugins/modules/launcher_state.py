#!/usr/bin/python
"""Read an ESP32 running Launcher through its serial console."""

DOCUMENTATION = r"""
module: launcher_state
short_description: What an ESP32 under Launcher holds, read through Launcher's serial console
description:
  - Reads Launcher's version, its partition table (its own partition, the installed apps with
    the slot the otadata selects, the data partitions, the free ranges, the flash size) and its
    settings (boot to app, boot timer, the WebUI user, the device name, the saved networks by
    ssid). No password is returned; a declared WebUI password is compared and the verdict said.
  - Launcher's console answers only while Launcher runs. When the board is in one of its apps
    and O(enter) is set, the broker attends Launcher's boot banner and the board is reset by
    the port's modem lines (a power-on, which Launcher's bootloader boots Launcher on); with
    O(leave) the app is booted again afterwards. A board that was in Launcher is left there.
  - Changes nothing on the device's flash. The same in check mode.
options:
  board:
    description: The board's name (its broker).
    type: str
    required: true
  socket:
    description: The broker's socket, when not the default for the board.
    type: path
  deadline:
    description: Seconds each console command may take.
    type: float
    default: 8.0
  enter:
    description: Reset into Launcher when its app has the port.
    type: bool
    default: true
  entry:
    description: How the boot banner is answered to stay in Launcher, C(selpress) (a key press) or
      C(settings) (bootToApp turned off for the boot and back on in the menu, for a build that loses
      the key press; the board type's C(launcher_enter)).
    type: str
    choices: [selpress, settings]
    default: selpress
  leave:
    description: Boot the selected app again when the read had to enter Launcher.
    type: bool
    default: true
  wait:
    description: Seconds to wait for Launcher's console after a reset.
    type: float
    default: 25.0
  wui_password:
    description: The declared WebUI password, compared and never returned.
    type: str
  wifi_psks:
    description: The declared networks' passwords, by ssid, compared and never returned.
    type: dict
  require:
    description: Fail when no Launcher answers (else C(launcher.running) is false).
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Read the board, entering Launcher if its app runs
  rxrbench.launcher.launcher_state:
    board: "{{ bench_board }}"
    wui_password: "{{ launcher_secrets[bench_board].wui_password }}"
  register: launcher
"""

RETURN = r"""
launcher:
  description: C(running) (Launcher answered), C(version), C(entered) (the read reset the board
    into Launcher), C(left) (and booted its app again), C(manager) (Launcher's partition),
    C(apps) (label, subtype, offset, size, boot), C(data), C(free), C(flash_size), C(free_total),
    C(boot) (the selected slot), C(settings) (without passwords), C(wui_password_matches),
    C(wifi_psk_matches) (by ssid, for the declared ones).
  returned: always
  type: dict
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.launcher.plugins.module_utils import console


def main():
    module = AnsibleModule(argument_spec={
        "board": {"type": "str", "required": True},
        "socket": {"type": "path"},
        "deadline": {"type": "float", "default": 8.0},
        "enter": {"type": "bool", "default": True},
        "entry": {"type": "str", "choices": list(console.ENTRIES), "default": "selpress"},
        "leave": {"type": "bool", "default": True},
        "wait": {"type": "float", "default": 25.0},
        "wui_password": {"type": "str", "no_log": True},
        "wifi_psks": {"type": "dict", "no_log": True},
        "require": {"type": "bool", "default": False},
    }, supports_check_mode=True)
    p = module.params
    out = {"running": False, "entered": False, "left": False}
    try:
        with broker_client.Board(p["board"], path=p["socket"], timeout=p["wait"] + 60) as b:
            if p["enter"]:
                _v, how = console.enter(b, wait=p["wait"], entry=p["entry"])
            else:
                how = "running" if console.version(b) else None
            if how:
                out.update(running=True, entered=how == "entered")
                out.update(console.facts(b, p["wui_password"], p["deadline"], wifi_psks=p["wifi_psks"]))
                if out["entered"] and p["leave"]:
                    console.leave(b)
                    out["left"] = True
    except (broker_client.BrokerError, console.ConsoleError) as e:
        unanswered = "port closed" in str(e) or "port is not open" in str(e)
        module.fail_json(msg=str(e), unanswered=unanswered, launcher=out)
    if p["require"] and not out["running"]:
        module.fail_json(msg=f"{p['board']}: no Launcher console answers", launcher=out)
    module.exit_json(changed=False, launcher=out)


if __name__ == "__main__":
    main()
