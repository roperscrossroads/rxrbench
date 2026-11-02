#!/usr/bin/python
"""Scenario steps on a stock Meshtastic node, through its phone API (module_utils/stocksteps.py).
No write gate: like device_run, the commands are the caller's (a text is sent, nothing is set).
"""
DOCUMENTATION = r"""
module: meshtastic_stock_step
short_description: Run scenario steps on a stock Meshtastic node through its phone API
description:
  - A stock node has no console, so a scenario step on it is one of the commands
    C(module_utils/stocksteps.py) speaks over its phone API, judged as C(device_run) judges a
    console's output (C(expect), C(refuse)).
  - "C(text <node> <message>): a direct text asking for an acknowledgement, waited for
    (C(ack from 0x<node>), C(nak ...), C(implicit ack ...), C(no answer ...)).
    C(nodes): its node database, newest first, a line per node as a zephyrtastic console's
    C(meshtastic nodedb list) prints it. C(info): the node itself. C(announce): its own
    NodeInfo broadcast now, so a node that lost its key (a fresh flash) can text it."
  - "C(within) asks again every second, on the same connection, until the step comes true; a
    text is sent once."
  - "The node is reached as its config is (C(meshtastic_stock_config)): its USB port by name
    (devreg resolve, guard, lock) or its network API. One that is not there fails with
    C(unanswered)."
options:
  board:
    description: The board's name in the layout.
    type: str
    required: true
  registry_name:
    description: Its registry name, when not the board's.
    type: str
  transport:
    description: Its USB port (C(serial)) or its network API (C(tcp)).
    type: str
    choices: [serial, tcp]
    default: serial
  host:
    description: (tcp) The node's address as the registry records it.
    type: str
  port:
    description: (tcp) The API's TCP port.
    type: int
    default: 4403
  bearer:
    description: (tcp) The registry route the address belongs to.
    type: str
    default: wifi
  devreg:
    description: devreg on this host.
    type: path
    default: ~/.devreg/devreg.py
  devlock_dir:
    description: Where devlock lives.
    type: path
    default: ~/.devreg
  connect_timeout:
    description: Seconds the library waits for the node.
    type: float
    default: 30
  open_deadline:
    description: Seconds to keep trying to open its API.
    type: float
    default: 60
  commands:
    description: The steps, as C(device_run) takes them (name, cmd, expect, refuse, timeout).
    type: list
    elements: dict
    required: true
  timeout:
    description: Seconds a text waits for its answer when the step names none.
    type: float
    default: 30
  within:
    description: Seconds a step is given to come true.
    type: int
    default: 0
"""
RETURN = r"""
results:
  description: Each step's name, cmd, output, passed and why (as C(device_run)).
  type: list
  returned: always
failed_tests:
  description: The names of the steps that did not pass.
  type: list
  returned: always
"""
import time

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.rxrbench.core.plugins.module_utils import runner
from ansible_collections.rxrbench.mesh.plugins.module_utils import netconfig, stocknode, stocksteps


def one(iface, c, p, sleep=time.sleep, clock=time.monotonic):
    """A step's result entry; asked again while it fails and `within` lasts (a text: once)."""
    entry = {"name": c["name"], "cmd": c["cmd"], "test": runner.is_test(c), "rc": None}
    end = clock() + p["within"]
    while True:
        try:
            output = stocksteps.run(iface, c["cmd"], float(c.get("timeout") or p["timeout"]))
            why = runner.judge(c, None, output)
        except stocksteps.StepError as e:
            output, why = "", [str(e)]
            break
        if not why or stocksteps.parse(c["cmd"])[0] in ("text", "announce") or clock() >= end:
            break
        sleep(1)
    entry.update(output=output, passed=not why, why=why)
    return entry


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "registry_name": {"type": "str"},
            "transport": {"type": "str", "choices": ["serial", "tcp"], "default": "serial"},
            "host": {"type": "str"},
            "port": {"type": "int", "default": netconfig.PORT},
            "bearer": {"type": "str", "default": "wifi"},
            "devreg": {"type": "path", "default": "~/.devreg/devreg.py"},
            "devlock_dir": {"type": "path", "default": "~/.devreg"},
            "connect_timeout": {"type": "float", "default": 30.0},
            "open_deadline": {"type": "float", "default": 60.0},
            "commands": {"type": "list", "elements": "dict", "required": True},
            "timeout": {"type": "float", "default": 30.0},
            "within": {"type": "int", "default": 0},
        },
        required_if=[("transport", "tcp", ("host",))],
        supports_check_mode=True,
    )
    p = module.params
    try:
        runner.check(p["commands"])
    except runner.RunnerError as e:
        module.fail_json(msg=str(e))
    session = stocknode.Session(p["transport"], p["board"], device=p["registry_name"],
                                host=p["host"], port=p["port"], bearer=p["bearer"],
                                devreg=p["devreg"], devlock_dir=p["devlock_dir"],
                                timeout=p["connect_timeout"], nodes=True)
    try:
        with session:
            iface = session.open(p["open_deadline"])
            try:
                results = [one(iface, c, p) for c in p["commands"]]
            finally:
                iface.close()
    except stocknode.NodeError as e:
        module.fail_json(msg=str(e), unanswered=isinstance(e, stocknode.Unanswered))
    failed = [r["name"] for r in results if not r["passed"]]
    module.exit_json(changed=False, results=results, failed_tests=failed)


if __name__ == "__main__":
    main()
