#!/usr/bin/python
"""Set a board's owner, device config, channels and admin keys through the phone API."""

DOCUMENTATION = r"""
module: meshtastic_config
short_description: Owner, device config, channels and admin keys of a board
description:
  - Brings a board's owner names, device role and rebroadcast mode, and channel table to what
    is declared. What the board has is read from its C(meshtastic state) line; what differs is
    written with the stock C(meshtastic) Python library through the firmware's phone API
    tunnel (C(meshtastic api)), as one edit transaction.
  - Only what is declared is managed. A channel slot that is not listed is left alone; to
    switch one off, list it with C(role=DISABLED).
  - After writing, the board is read again until it shows the declared state (it may restart
    to apply it); the module fails if it does not within C(settle) seconds.
  - Keys are never returned or shown. A diff shows a channel key as its fingerprint. An admin
    key is a public key and is shown.
  - A managed node (C(security.is_managed)) takes configuration only from a remote admin, and
    nothing local can switch that off again. So C(is_managed) is written last and by itself,
    after everything else has been read back, and a managed board that differs from what is
    declared is refused (in check mode too), with what differs in C(written).
  - Supports check mode and diff. A run that writes needs the C(meshtastic) library in the
    module's Python; a run that finds nothing to change does not.
options:
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode), a read and a board that already matches need none.
    type: bool
    default: false
  board:
    description: The board's name.
    type: str
    required: true
  owner:
    description: The owner names. Either may be left out.
    type: dict
    suboptions:
      long_name: {type: str, description: The long name.}
      short_name: {type: str, description: "The short name, at most 4 bytes."}
  device:
    description: Device config, by its stock names.
    type: dict
    suboptions:
      role: {type: str, description: The device role.}
      rebroadcast_mode: {type: str, description: The rebroadcast mode.}
  lora:
    description: LoRa config the phone API carries and the shell does not. Only
      C(hop_limit) (1 to 7). Read through the tunnel, so planning needs the C(meshtastic) library.
    type: dict
    suboptions:
      hop_limit: {type: int, description: The hop limit of packets this node originates.}
  security:
    description: Security config, by its stock names. Read through the tunnel, so planning
      needs the C(meshtastic) library. The identity key pair is not managed here
      (M(rxrbench.mesh.meshtastic_identity)).
    type: dict
    suboptions:
      admin_key:
        description: The public keys of the nodes that may administer this one remotely, each
          C(base64:<key>) or C(hex:<key>) (32 bytes), at most 3. The node is given exactly
          these; an empty list removes all. Left alone if absent.
        type: list
        elements: str
      is_managed:
        description: Whether the node takes configuration only from a remote admin. C(true)
          needs an C(admin_key) declared beside it. Left alone if absent.
        type: bool
  channels:
    description: The channels to manage.
    type: list
    elements: dict
    suboptions:
      index: {type: int, required: true, description: The slot.}
      role: {type: str, required: true, choices: [PRIMARY, SECONDARY, DISABLED], description: The role.}
      name: {type: str, description: The channel name.}
      psk:
        description: C(default), C(none), C(base64:<key>) or C(hex:<key>) (16 or 32 bytes).
        type: str
        default: default
      uplink_enabled: {type: bool, description: MQTT uplink. Left alone if absent.}
      downlink_enabled: {type: bool, description: MQTT downlink. Left alone if absent.}
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  deadline:
    description: Seconds allowed for each shell command.
    type: float
    default: 20.0
  settle:
    description: Seconds the board has to show the declared state after a write.
    type: float
    default: 30.0
  restart_wait:
    description: Seconds to wait for the restart the firmware schedules after a security write
      (a few seconds after it answers), so the next step does not meet a board going down.
      The restart is seen as the console going down and coming back; if none is seen in this
      time the module goes on. C(0) does not wait.
    type: float
    default: 20.0
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Owner, device and channels
  rxrbench.mesh.meshtastic_config:
    board: "{{ bench_board }}"
    owner: {long_name: board1, short_name: brd1}
    device: {role: CLIENT, rebroadcast_mode: ALL}
    channels:
      - {index: 0, name: ShortTurbo, role: PRIMARY, psk: default}
      - {index: 2, name: cluster, role: SECONDARY, psk: "{{ bench_secrets.c1.cluster }}"}
"""

RETURN = r"""
written:
  description: What was (or, in check mode, would be) written, by section. Channels by index.
  returned: always
  type: dict
  sample: {owner: true, device: [rebroadcast_mode], channels: [0, 2], security: [admin_key]}
"""

import time

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import config, state, tunnel


class TooOld(Exception):
    """The firmware predates the state line: its configuration cannot be read."""


def read(board, deadline):
    facts = state.read(board, deadline)
    if facts is None:
        raise TooOld()
    return facts


def read_api(b, deadline):
    """The fields only the phone API shows, read through the tunnel: (lora_api, security_api)."""
    if not tunnel.HAS_MESHTASTIC:
        raise config.ConfigError("the `meshtastic` Python library is not installed for this "
                                 "module's interpreter; it is needed to read lora.hop_limit "
                                 "and the security config")
    iface = tunnel.TunnelInterface(lambda cmd: b.exec(cmd, deadline=deadline), noNodes=True,
                                   poll_interval=0.2)
    try:
        local = iface.localNode.localConfig
        return {"hop_limit": int(local.lora.hop_limit)}, config.security_api(local.security)
    finally:
        iface.close()


def await_restart(b, deadline, window, sleep=time.sleep, clock=time.monotonic):
    """Wait for the restart a security write schedules: the console goes down, then the state
    line answers again and shows what was written (the caller checks that). Returns True when
    a restart was seen, False when the board answered through the whole window."""
    end, went_down = clock() + window, False
    opened = broker_client.opens(b)
    while clock() < end:
        try:
            state.read(b, deadline, attempts=1)
            # the broker reopens a closed port for the next command, so a restart between two
            # reads may show only as the broker's count of opens going up
            if went_down or broker_client.reopened(b, opened):
                return True
        except (broker_client.BrokerError, state.StateError):
            went_down = True
        sleep(0.5)
    return False


def summary(changes):
    out = {}
    if "lora" in changes:
        out["lora"] = sorted(changes["lora"])
    if "owner" in changes:
        out["owner"] = True
    if "device" in changes:
        out["device"] = sorted(changes["device"])
    if "channels" in changes:
        out["channels"] = [c["index"] for c in changes["channels"]]
    if "security" in changes:
        out["security"] = sorted(changes["security"])
    return out


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "board": {"type": "str", "required": True},
            "owner": {"type": "dict", "options": {
                "long_name": {"type": "str"}, "short_name": {"type": "str"}}},
            "device": {"type": "dict", "options": {
                "role": {"type": "str"}, "rebroadcast_mode": {"type": "str"}}},
            "lora": {"type": "dict", "options": {"hop_limit": {"type": "int"}}},
            "security": {"type": "dict", "options": {
                "admin_key": {"type": "list", "elements": "str"},
                "is_managed": {"type": "bool"}}},
            "channels": {"type": "list", "elements": "dict", "options": {
                "index": {"type": "int", "required": True},
                "role": {"type": "str", "required": True,
                         "choices": ["PRIMARY", "SECONDARY", "DISABLED"]},
                "name": {"type": "str"},
                "psk": {"type": "str", "default": "default", "no_log": True},
                "uplink_enabled": {"type": "bool"},
                "downlink_enabled": {"type": "bool"}}},
            "socket": {"type": "path"},
            "deadline": {"type": "float", "default": 20.0},
            "settle": {"type": "float", "default": 30.0},
            "restart_wait": {"type": "float", "default": 20.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    desired = {
        "owner": p["owner"] or {},
        "device": {k: v for k, v in (p["device"] or {}).items() if v is not None},
        "lora": {k: v for k, v in (p["lora"] or {}).items() if v is not None},
        "security": {k: v for k, v in (p["security"] or {}).items() if v is not None},
        "channels": [{k: v for k, v in ch.items() if v is not None} for ch in p["channels"] or []],
    }

    def facts_of(b):
        facts = read(b, p["deadline"])
        if desired["lora"] or desired["security"]:
            facts["lora_api"], facts["security_api"] = read_api(b, p["deadline"])
        return facts

    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            facts = facts_of(b)
            changes, diff = config.plan(desired, facts)
            result = {"changed": bool(changes), "written": summary(changes)}
            if changes:
                result["diff"] = diff
            refusal = config.managed_refusal(changes, facts)
            if refusal:
                module.fail_json(msg=f"{p['board']}: {refusal}", **result)
            if not changes or module.check_mode:
                module.exit_json(**result)
            gate.check(module)   # the layout's go-ahead, before the first write
            if not tunnel.HAS_MESHTASTIC:
                module.fail_json(msg="the `meshtastic` Python library is not installed for "
                                     "this module's interpreter; it is needed to write")

            def run(cmd):
                return b.exec(cmd, deadline=p["deadline"])

            # Each phase is written, then the node is read until it shows it (it saves on a
            # short delay and may restart to apply), before the next is sent: the security
            # section goes last, because a node that becomes managed takes nothing after it.
            for phase in config.phases(changes):
                iface = tunnel.TunnelInterface(run, noNodes=True, poll_interval=0.2)
                try:
                    config.write(iface, phase)
                finally:
                    iface.close()
                end = time.monotonic() + p["settle"]
                left, why = phase, None
                while left and time.monotonic() < end:
                    try:
                        left = {k: v for k, v in config.plan(desired, facts_of(b))[0].items()
                                if k in phase}
                        why = None
                    except (broker_client.BrokerError, state.StateError, tunnel.TunnelError,
                            config.ConfigError) as e:
                        why = str(e)
                    if left:
                        time.sleep(0.5)
                if left:
                    module.fail_json(msg="the board did not take the configuration: "
                                         + (why or f"still differs in {summary(left)}"),
                                     written=result["written"])
                if "security" in phase and p["restart_wait"] > 0:
                    # The firmware restarts a few seconds after it stores a security config.
                    # What it shows before that is what it holds in memory: wait the restart
                    # out, then read once more, so what is reported is what was kept.
                    result["restarted"] = await_restart(b, p["deadline"], p["restart_wait"])
                    kept = {k: v for k, v in config.plan(desired, facts_of(b))[0].items()
                            if k in phase}
                    if kept:
                        module.fail_json(msg="the board did not keep the configuration through "
                                             f"its restart: differs in {summary(kept)}",
                                         written=result["written"])
            module.exit_json(**result)
    except TooOld:
        if module.check_mode:
            # A plan made before the board is flashed: say so, do not guess.
            module.exit_json(changed=True, written={"unknown": "until flashed"},
                             msg="this firmware has no `meshtastic state`: its configuration "
                                 "is unknown until the board is flashed")
        module.fail_json(msg="this firmware has no `meshtastic state`; flash it first")
    except (broker_client.BrokerError, state.StateError, config.ConfigError,
            tunnel.TunnelError) as e:
        module.fail_json(msg=str(e))
    except Exception as e:  # pylint: disable=broad-except
        # The stock library raises its own types (a connect that times out, a refused admin).
        module.fail_json(msg=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
