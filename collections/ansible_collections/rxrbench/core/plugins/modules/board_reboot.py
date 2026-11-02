#!/usr/bin/python
"""Reboot one device through its console or over ssh and wait for it to answer again.

No write gate (module_utils/gate.py): a reboot changes no stored state.
"""

DOCUMENTATION = r"""
module: board_reboot
short_description: Reboot a device through its console or over ssh, and wait until it is back
description:
  - "Sends the reboot command of the board's firmware family through its serial broker, or
    over ssh when C(route) is given, then waits until the board answers again. The reboot is
    proven, not assumed: the console went down and came back, or the board's uptime is less
    than it would be had it kept running, or (a family that cannot say its uptime) the board
    printed its family's boot banner and then answered the family's C(ready) command."
  - "Writes nothing to the board. A family this tool cannot reboot through a console, or whose
    reboot it could not tell from none (no uptime, no banner or ready command), is refused,
    with the reason."
  - In check mode the board is read (its uptime, where it says one) and nothing is sent.
options:
  board:
    description: The board's name.
    type: str
    required: true
  family:
    description: The firmware family the board runs.
    type: str
    required: true
  settle:
    description: Seconds to wait for the board to answer again.
    type: float
    default: 60
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  route:
    description: The device's ssh route (C(bench_routes.shell)), for a device whose shell is
      ssh. Its console is then not used.
    type: dict
  families:
    description: The device families as data, the inventory's C(bench_families). It says how
      a board's identity is read and what an image of a family must match.
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Reboot
  rxrbench.core.board_reboot:
    board: "{{ bench_board }}"
    family: "{{ bench_family }}"
"""

RETURN = r"""
before_s:
  description: The board's uptime when asked, in seconds.
  returned: always
  type: float
after_s:
  description: The board's uptime at its first answer after the reboot.
  returned: when rebooted
  type: float
took_s:
  description: Seconds from the command to that answer.
  returned: when rebooted
  type: float
resets:
  description: What the board says about its last reset, where its family has a command for it.
  returned: when rebooted and the family has one
  type: list
  elements: str
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, familyspec, reboot, ssh, stateline


def main():
    module = AnsibleModule(
        argument_spec={
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "family": {"type": "str", "required": True},
            "settle": {"type": "float", "default": 60.0},
            "socket": {"type": "path"},
            "route": {"type": "dict"},
        },
        supports_check_mode=True,
    )
    p = module.params
    familyspec.load(p["families"])
    def through(b):
        if module.check_mode:
            if reboot.commands(p["family"]).get("uptime") is None:
                module.exit_json(changed=True, msg=f"{p['board']}: would be rebooted (it does "
                                                   "not say how long it has been up)")
            up = reboot.uptime(b, p["family"])
            module.exit_json(changed=True, before_s=up,
                             msg=f"{p['board']}: up {up:.0f} s, would be rebooted")
        r = reboot.reboot(b, p["family"], p["settle"])
        cmd = familyspec.of(p["family"])["reset_command"]
        if cmd:
            try:
                # its first paragraph: this boot; the history after it stays on the board
                r["resets"] = b.exec(cmd).strip().split("\n\n")[0].splitlines()
            except (broker_client.BrokerError, ssh.SshError) as e:
                r["resets"] = [f"`{cmd}`: {e}"]
        return r

    try:
        if p["route"]:
            r = through(ssh.Console(ssh.Target(p["route"])))
        else:
            with broker_client.Board(p["board"], path=p["socket"]) as b:
                r = through(b)
    except (broker_client.BrokerError, ssh.SshError, reboot.RebootError, stateline.StateError) as e:
        module.fail_json(msg=f"{p['board']}: {e}")
    was = f"was up {r['before_s']:.0f} s" if r.get("before_s") is not None \
        else f"seen by {r.get('seen_by')}"
    module.exit_json(changed=True, msg=f"{p['board']}: rebooted, back after {r['took_s']} s "
                                       f"({was})", **r)


if __name__ == "__main__":
    main()
