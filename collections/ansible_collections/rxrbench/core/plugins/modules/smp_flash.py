#!/usr/bin/python
"""Update a Zephyr device over MCUmgr."""

DOCUMENTATION = r"""
module: smp_flash
short_description: Update a Zephyr + MCUboot device over MCUmgr (SMP)
description:
  - Uploads a signed image into the device's spare slot over MCUmgr, marks it for a test
    boot, resets the device, and confirms the image once it runs. If the new image does not
    come up, MCUboot swaps back and the module fails saying the device runs its previous image.
  - Decides by the image's hash, which MCUmgr reports for each slot. A device whose running
    image is the image is not touched.
  - "Refuses, before anything is uploaded: an image for another board type or class, and a
    device whose running image is not yet confirmed."
  - C(serial) is MCUmgr over the shell's UART; the board's serial broker releases the console
    for each session. C(ble) connects to C(address), holding the host's Bluetooth adapter
    (devlock resource C(ble:<adapter>)) for every session, and repairs a stale bond at first
    contact unless C(rebond) is C(never). C(udp) reaches a board on a network at C(address)
    (its IP) and C(udp_port); the address is checked against the registry's for the route
    named by C(bearer) (C(eth) or C(wifi)) before any session.
  - Runs on the bench host, with a Python that has C(smpclient).
  - Supports check mode and diff.
options:
  board:
    description: The board's name.
    type: str
    required: true
  image:
    description: The image directory (C(manifest.json) and C(app.signed.bin)), on this host.
    type: path
    required: true
  transport:
    description: How MCUmgr reaches the device.
    type: str
    choices: [serial, ble, udp]
    default: serial
  address:
    description: C(ble) the device's Bluetooth address; C(udp) its IP address.
    type: str
  bearer:
    description: C(udp) only. The registry route the address is the registry's for (C(eth), C(wifi)).
    type: str
  udp_port:
    description: C(udp) only. MCUmgr's UDP port on the device.
    type: int
    default: 1337
  board_tag:
    description: The board's type, from the device registry. The image must be for it.
    type: str
  declared:
    description: "The board's C(firmware) block, as the layout declares it. The family says what the
      image must match (a class and features, a role: the family says which); a mismatch is a refusal."
    type: dict
  devreg:
    description: The device registry tool on this host.
    type: path
    default: ~/.devreg/devreg.py
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  settle:
    description: Seconds the device has to come back running the new image.
    type: float
    default: 240.0
  devlock_dir:
    description: Where devlock.py lives on this host (the adapter lock).
    type: path
    default: ~/.devreg
  adapter:
    description: C(ble) only. The host's Bluetooth adapter, the resource that is locked.
    type: str
    default: hci0
  ble_lock_wait:
    description: C(ble) only. Seconds to wait for a busy adapter before refusing.
    type: float
    default: 300.0
  rebond:
    description:
      - C(ble) only. C(auto) removes this host's bond for the device and retries once when
        the first exchange fails on a device BlueZ shows as paired and the board, asked on
        its console (C(console)), holds no bond for this host (a reflashed board has lost its
        bonds). A board that holds the bond too, or cannot be asked, keeps it, and the error
        says which. A failure to connect is repaired on the same condition, and never otherwise.
        C(force) removes it without asking the board, after an exchange that failed. C(never) leaves bonds
        alone. A plan (check mode) never removes a bond.
    type: str
    choices: [auto, never, force]
    default: auto
  port_from:
    description:
      - Where the serial port comes from. C(devreg) resolves the board's USB port by name;
        C(broker) uses whatever pty the board's broker releases (a simulated board).
    type: str
    choices: [devreg, broker]
    default: devreg
  run_dir:
    description: A simulated board's run directory (C(bench_sim_run)), which marks it as one.
    type: path
  console:
    description:
      - C(ble) only. The board also has a console behind its serial broker on this host.
        The bond repair asks the board through it, and after the swap the module waits for
        it to answer, so the next task does not find the port closed.
    type: bool
    default: false
  flash_unknown:
    description: Write a board that answers as its family but reports neither a build nor
      a hash, so nothing can tell whether it already runs the image. On by default (a roll
      from old firmware needs it); a layout turns it off with C(bench_flash_unknown), and
      such a board is then refused.
    type: bool
    default: true
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode) needs none.
    type: bool
    default: false
  families:
    description: The device families as data, the inventory's C(bench_families). It says how
      a board's identity is read and what an image of a family must match.
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
extends_documentation_fragment:
  - rxrbench.core.check_depth
"""

EXAMPLES = r"""
- name: Update over MCUmgr
  rxrbench.core.smp_flash:
    board: "{{ bench_board }}"
    image: "{{ board_host_store }}/{{ bench_image | basename }}"
    board_tag: "{{ bench_board_tag }}"
    declared: "{{ firmware }}"
  vars:
    ansible_python_interpreter: /home/bench/.venv-mcumgr/bin/python
"""

RETURN = r"""
image:
  description: What the image is.
  returned: always
  type: dict
device:
  description: What the device runs after the run (in check mode, what it runs now).
  returned: success
  type: dict
slot:
  description: The running slot's flags (slot, confirmed, pending).
  returned: success
  type: dict
rebonded:
  description: Why this host's bond for the device was removed and replaced, when it was.
  returned: when a stale bond was repaired
  type: str
timing:
  description: Each phase of the run (resolve, read, preflight, write, verify, leave) and its duration in seconds, as C(module_utils/timing.py) records it.
  returned: success
  type: list
  elements: dict
checked:
  description: The depth of the checks before the write (C(full) or C(spot)) and why (docs/ARCHITECTURE.md#checking-depth).
  returned: success
  type: dict
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, smp


def main():
    module = AnsibleModule(
        argument_spec={
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "image": {"type": "path", "required": True},
            "transport": {"type": "str", "choices": ["serial", "ble", "udp"], "default": "serial"},
            "address": {"type": "str"},
            "bearer": {"type": "str"},
            "udp_port": {"type": "int", "default": 1337},
            "board_tag": {"type": "str"},
            "declared": {"type": "dict"},
            "devreg": {"type": "path", "default": "~/.devreg/devreg.py"},
            "socket": {"type": "path"},
            "settle": {"type": "float", "default": 240.0},
            "devlock_dir": {"type": "path", "default": "~/.devreg"},
            "adapter": {"type": "str", "default": "hci0"},
            "ble_lock_wait": {"type": "float", "default": 300.0},
            "rebond": {"type": "str", "choices": ["auto", "never", "force"], "default": "auto"},
            "console": {"type": "bool", "default": False},
            "port_from": {"type": "str", "choices": ["devreg", "broker"], "default": "devreg"},
            "run_dir": {"type": "path"},
            "flash_unknown": {"type": "bool", "default": True},
            "check_depth": {"type": "str", "choices": ["full", "spot"], "default": "full"},
            **gate.ARGS,
        },
        required_if=[("transport", "ble", ["address"]), ("transport", "udp", ["address", "bearer"])],
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    familyspec.load(p["families"])
    try:
        manifest, _file = common.load_image(p["image"])
        kw = {"transport": p["transport"], "address": p["address"], "board_tag": p["board_tag"],
              "bearer": p["bearer"], "udp_port": p["udp_port"],
              "port_from": p["port_from"], "run_dir": p["run_dir"],
              "declared": p["declared"], "devreg": p["devreg"], "settle": p["settle"],
              "devlock_dir": p["devlock_dir"], "adapter": p["adapter"],
              "ble_lock_wait": p["ble_lock_wait"],
              # A plan changes nothing, and that includes this host's bonds.
              "rebond": "never" if module.check_mode else p["rebond"]}
        if p["transport"] == "serial":
            with broker_client.Board(p["board"], path=p["socket"], timeout=p["settle"] + 60) as b:
                flasher = smp.SmpFlasher(p["board"], manifest, p["image"], board=b, **kw)
                r = common.drive(flasher, manifest, check_mode=module.check_mode, check_depth=p["check_depth"],
                             flash_unknown=p["flash_unknown"])
        elif p["console"]:
            with broker_client.Board(p["board"], path=p["socket"], timeout=p["settle"] + 60) as b:
                flasher = smp.SmpFlasher(p["board"], manifest, p["image"], console=b, **kw)
                r = common.drive(flasher, manifest, check_mode=module.check_mode, check_depth=p["check_depth"],
                                 flash_unknown=p["flash_unknown"])
        else:
            flasher = smp.SmpFlasher(p["board"], manifest, p["image"], **kw)
            r = common.drive(flasher, manifest, check_mode=module.check_mode, check_depth=p["check_depth"],
                             flash_unknown=p["flash_unknown"])
    except (common.FlashError, broker_client.BrokerError, ValueError) as e:
        # `unanswered`: the device is not there to read (the play skips it, not the bench)
        module.fail_json(msg=str(e), unanswered=isinstance(e, common.Unanswered))
    result = {"changed": r["changed"], "image": r["image"], "slot": flasher.flags,
              "device": r["before"] if module.check_mode else r["after"]}
    if flasher.rebonded:
        result["rebonded"] = flasher.rebonded
    result["timing"] = r.get("timing", [])
    result["checked"] = r.get("checked")
    result["identity_checked"] = r.get("identity_checked")
    if r["changed"]:
        result["why"] = r["why"]
        result["diff"] = {"before": {k: v for k, v in r["before"].items() if v is not None},
                          "after": {k: v for k, v in r["after"].items() if v is not None}}
    module.exit_json(**result)


if __name__ == "__main__":
    main()
