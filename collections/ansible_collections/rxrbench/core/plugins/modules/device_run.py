#!/usr/bin/python
"""Run named commands on a device, collect their output, and judge it.

No write gate (module_utils/gate.py): the commands are the caller's, diagnostics and tests.
"""

DOCUMENTATION = r"""
module: device_run
short_description: Run commands on a device and collect or judge their output
description:
  - Runs a list of named commands on a device over its shell route and returns each one's
    output. This is what pulling diagnostics and running tests have in common.
  - "A command with C(expect), C(refuse) or C(rc) is a test: its output must match C(expect),
    must not match C(refuse), and (where the device has exit codes) it must exit with C(rc).
    A command with none of them is a diagnostic, and only fails if it cannot be run."
  - A board is reached through its serial broker (C(board), C(socket)); its shell has no
    exit codes. A Linux device is reached over ssh (C(route)) from the host this module runs on.
  - The commands are the caller's. This module adds nothing to them and does not know which
    ones change a device, so it reports C(changed=false); it does not run in check mode
    unless C(read_only) says the commands are safe to.
options:
  commands:
    description: The commands, in order.
    type: list
    elements: dict
    required: true
    suboptions:
      name: {type: str, required: true, description: A short name; it becomes a file name.}
      cmd: {type: str, required: true, description: The command line.}
      expect: {type: str, description: A regular expression the output must match.}
      refuse: {type: str, description: A regular expression the output must not match.}
      rc: {type: int, description: The exit code wanted (Linux only). A test defaults to 0.}
      timeout: {type: float, description: Seconds allowed for this command.}
  board:
    description: A board's name, to reach it through its serial broker.
    type: str
  dialect:
    description: How the board's console answers (its family's C(console), as the broker knows
      it); the default is a shell read to its prompt. A console that answers one line per
      command needs it named, or every command times out.
    type: str
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  route:
    description: A Linux device's ssh route (C(bench_routes.shell)).
    type: dict
  console_tail:
    description: Boards only. Also return the last this-many bytes of the board's console
      log, as a result named C(console). 0 returns none.
    type: int
    default: 0
  read_only:
    description: The commands only read, so they may run in check mode too.
    type: bool
    default: false
  fail_on_failure:
    description: Fail the task when a test fails. Off, the failures are only reported.
    type: bool
    default: true
  timeout:
    description: Seconds allowed for a command that names no timeout of its own.
    type: float
    default: 20.0
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: The device answers and runs what it should
  rxrbench.core.device_run:
    board: "{{ bench_board }}"
    commands:
      - {name: version, cmd: kernel version, expect: "^Zephyr version"}
      - {name: uptime, cmd: kernel uptime}
"""

RETURN = r"""
results:
  description: One entry per command (name, cmd, output, rc, test, passed, why).
  returned: always
  type: list
  elements: dict
failed_tests:
  description: The names of the tests that failed.
  returned: always
  type: list
  elements: str
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, runner, ssh


def console_tail(path, size):
    """The end of the board's console log, which its broker keeps on this host."""
    entry = {"name": "console", "cmd": f"(the last {size} bytes of {path})", "test": False,
             "rc": None, "passed": True, "why": []}
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - size))
            entry["output"] = f.read().decode(errors="replace")
    except (OSError, TypeError) as e:
        entry.update(output="", passed=False, why=[f"cannot read the console log: {e}"])
    return entry


def main():
    module = AnsibleModule(
        argument_spec={
            "commands": {"type": "list", "elements": "dict", "required": True, "options": {
                "name": {"type": "str", "required": True},
                "cmd": {"type": "str", "required": True},
                "expect": {"type": "str"}, "refuse": {"type": "str"},
                "rc": {"type": "int"}, "timeout": {"type": "float"}}},
            "board": {"type": "str"},
            "socket": {"type": "path"},
            "dialect": {"type": "str"},
            "route": {"type": "dict"},
            "console_tail": {"type": "int", "default": 0},
            "read_only": {"type": "bool", "default": False},
            "fail_on_failure": {"type": "bool", "default": True},
            "timeout": {"type": "float", "default": 20.0},
        },
        mutually_exclusive=[("board", "route")],
        required_one_of=[("board", "route")],
        supports_check_mode=True,
    )
    p = module.params
    commands = [{k: v for k, v in c.items() if v is not None} for c in p["commands"]]
    # a play hands a family with no console kind on as an empty value
    p["dialect"] = None if p["dialect"] in ("", "None") else p["dialect"]
    try:
        runner.check(commands)
    except runner.RunnerError as e:
        module.fail_json(msg=str(e))
    if module.check_mode and not p["read_only"]:
        module.exit_json(changed=False, skipped=True, results=[], failed_tests=[],
                         msg="not run in check mode (the commands are not declared read_only)")
    try:
        if p["board"]:
            with broker_client.Board(p["board"], path=p["socket"], timeout=p["timeout"] + 60) as b:
                results = runner.run_all(
                    commands, lambda cmd, t: (None, b.exec(cmd, deadline=t, **(
                        {"dialect": p["dialect"]} if p["dialect"] else {}))), p["timeout"])
                if p["console_tail"] > 0:
                    results.append(console_tail(b.status().get("log"), p["console_tail"]))
        else:
            target = ssh.Target(p["route"])
            results = runner.run_all(commands, target.run, p["timeout"])
    except (broker_client.BrokerError, ssh.SshError) as e:
        module.fail_json(msg=str(e))
    failed = [r["name"] for r in results if not r["passed"]]
    out = {"changed": False, "results": results, "failed_tests": failed}
    if failed and p["fail_on_failure"]:
        module.fail_json(msg="failed: " + "; ".join(
            f"{r['name']} ({', '.join(r['why'])})" for r in results if not r["passed"]), **out)
    module.exit_json(**out)


if __name__ == "__main__":
    main()
