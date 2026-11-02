#!/usr/bin/python
"""Configure a stock Meshtastic node over its network API (WiFi or Ethernet, TCP 4403)."""

DOCUMENTATION = r"""
module: meshtastic_net_config
short_description: Owner, device, display, channels and admin keys of a stock node, over TCP
description:
  - Brings a stock Meshtastic node that is on a network (WiFi or Ethernet, the API on TCP 4403)
    to what is declared. What the node has is read from its phone API with the stock
    C(meshtastic) library's TCPInterface (its own config and channels; the node database is
    not downloaded); what differs is written as one edit transaction, as
    M(rxrbench.mesh.meshtastic_config) does through a console.
  - The same sections as M(rxrbench.mesh.meshtastic_config), and C(display), which only the
    phone API reaches (C(displaymode) picks the UI on a board whose firmware carries two).
  - Only what is declared is managed. A channel slot that is not listed is left alone.
  - The radio (region, preset, power) is not managed here.
  - The node restarts to apply most writes and drops the connection; it is connected to
    again and read until it shows the declared state, or the module fails after C(settle)
    seconds. A managed node (C(security.is_managed)) that differs is refused, and
    C(is_managed) is written last, by itself.
  - Keys are never returned or shown; a diff shows a channel key as its fingerprint.
  - With nothing declared it only reads (C(facts)). Supports check mode and diff.
options:
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode), a read and a board that already matches need none.
    type: bool
    default: false
  host:
    description: The node's address or name.
    type: str
    required: true
  port:
    description: The API's TCP port.
    type: int
    default: 4403
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
    description: Only C(hop_limit) (1 to 7).
    type: dict
    suboptions:
      hop_limit: {type: int, description: The hop limit of packets this node originates.}
  display:
    description: Display config, by its stock names.
    type: dict
    suboptions:
      displaymode:
        description: C(DEFAULT), C(TWOCOLOR), C(INVERTED) or C(COLOR). On a board whose
          firmware carries both UIs, C(COLOR) selects the touch/menu UI and C(DEFAULT) the
          button UI. The stock ESP32 firmware serves no network API under C(COLOR), so that
          switch is written last and is confirmed only by the API going away (a warning says
          so); coming back to the button UI is done on the device.
        type: str
        choices: [DEFAULT, TWOCOLOR, INVERTED, COLOR]
      screen_on_secs: {type: int, description: Seconds the screen stays on.}
  security:
    description: As in M(rxrbench.mesh.meshtastic_config).
    type: dict
    suboptions:
      admin_key:
        description: The public keys that may administer the node, C(base64:<key>) or
          C(hex:<key>), at most 3. An empty list removes all.
        type: list
        elements: str
      is_managed:
        description: Whether the node takes configuration only from a remote admin.
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
  connect_timeout:
    description: Seconds a connection and its configuration download may take.
    type: int
    default: 30
  flush:
    description: Seconds to hold the connection open after a write, before closing it, so the
      node has taken every message before the connection goes away.
    type: float
    default: 6.0
  settle:
    description: Seconds the node has to come back and show the declared state after a write.
    type: float
    default: 120.0
  api_off_grace:
    description: After a switch to C(displaymode=COLOR), seconds the API must stay unanswered
      before the switch is taken as done (the firmware serves no API under that mode). A node
      that comes back instead is read and compared as after any write.
    type: float
    default: 45.0
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: The menu UI, and the bench's channels
  rxrbench.mesh.meshtastic_net_config:
    host: 192.0.2.40
    display: {displaymode: COLOR}
    channels:
      - {index: 0, name: ShortTurbo, role: PRIMARY, psk: default}
"""

RETURN = r"""
api_closed:
  description: The node stopped answering on its API after a switch to C(COLOR), as the
    firmware does under that mode; that switch was not read back.
  returned: always
  type: bool
written:
  description: What was (or, in check mode, would be) written, by section. Channels by index.
  returned: always
  type: dict
  sample: {display: [displaymode], channels: [0]}
facts:
  description: What the node showed last (node id, firmware, owner, device, display, channels
    with keys as fingerprints, the hop limit, the admin keys, managed).
  returned: always
  type: dict
"""

import time

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.mesh.plugins.module_utils import config, netconfig

try:
    import meshtastic  # noqa: F401 pylint: disable=unused-import
    HAS_MESHTASTIC = True
except ImportError:
    HAS_MESHTASTIC = False


def summary(changes):
    out = {}
    for k, v in changes.items():
        if k == "owner":
            out[k] = True
        elif k == "channels":
            out[k] = [c["index"] for c in v]
        else:
            out[k] = sorted(v)
    return out


def read(p):
    iface = netconfig.connect(p["host"], p["port"], p["connect_timeout"])
    try:
        return netconfig.facts_of(iface)
    finally:
        iface.close()


def write(p, phase, sleep=time.sleep):
    iface = netconfig.connect(p["host"], p["port"], p["connect_timeout"])
    try:
        config.write(iface, phase)
        sleep(p["flush"])
    finally:
        iface.close()


def settle(p, desired, phase, sleep=time.sleep, clock=time.monotonic):
    """Read the node until it shows `phase` written. A node that is restarting refuses the
    connection or drops it mid-download; that is waited out. Returns (facts, what still
    differs, the last error)."""
    end, facts, left, why = clock() + p["settle"], None, phase, None
    sleep(3)
    while clock() < end:
        try:
            facts = read(p)
            left = {k: v for k, v in netconfig.plan(desired, facts)[0].items() if k in phase}
            why = None
            if not left:
                break
        except Exception as e:  # pylint: disable=broad-except
            # the stock library raises its own types (a connect refused, a download cut off)
            why = f"{type(e).__name__}: {e}"
        sleep(3)
    return facts, left, why


def settle_closing(p, desired, phase, sleep=time.sleep, clock=time.monotonic):
    """settle() for a switch to a display mode the node serves no API under. The node answers
    with its old state until it restarts; then it either stays away for `api_off_grace` seconds
    (taken as done) or comes back and is compared as usual (a build without that UI coerces the
    mode back, which is reported as not taken). Returns (facts, what still differs, the last
    error, whether the API closed)."""
    end, facts, left, why, down_since = clock() + p["settle"], None, phase, None, None
    sleep(3)
    while clock() < end:
        try:
            facts = read(p)
            down_since = None
            left = {k: v for k, v in netconfig.plan(desired, facts)[0].items() if k in phase}
            why = None
            if not left:
                return facts, {}, None, False
        except Exception as e:  # pylint: disable=broad-except
            why = f"{type(e).__name__}: {e}"
            down_since = clock() if down_since is None else down_since
            if clock() - down_since >= p["api_off_grace"]:
                return facts, {}, None, True
        sleep(3)
    return facts, left, why, False


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "host": {"type": "str", "required": True},
            "port": {"type": "int", "default": netconfig.PORT},
            "owner": {"type": "dict", "options": {
                "long_name": {"type": "str"}, "short_name": {"type": "str"}}},
            "device": {"type": "dict", "options": {
                "role": {"type": "str"}, "rebroadcast_mode": {"type": "str"}}},
            "lora": {"type": "dict", "options": {"hop_limit": {"type": "int"}}},
            "display": {"type": "dict", "options": {
                "displaymode": {"type": "str",
                                "choices": list(netconfig.DISPLAY_FIELDS["displaymode"])},
                "screen_on_secs": {"type": "int"}}},
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
            "connect_timeout": {"type": "int", "default": 30},
            "flush": {"type": "float", "default": 6.0},
            "settle": {"type": "float", "default": 120.0},
            "api_off_grace": {"type": "float", "default": 45.0},
        },
        supports_check_mode=True,
    )
    p = module.params
    if not HAS_MESHTASTIC:
        module.fail_json(msg="the `meshtastic` Python library is not installed for this "
                             "module's interpreter; it is how a node on the network is read")
    desired = {
        sec: {k: v for k, v in (p[sec] or {}).items() if v is not None}
        for sec in ("owner", "device", "lora", "display", "security")
    }
    desired["channels"] = [{k: v for k, v in ch.items() if v is not None}
                           for ch in p["channels"] or []]
    try:
        facts = read(p)
        changes, diff = netconfig.plan(desired, facts)
    except config.ConfigError as e:
        module.fail_json(msg=str(e))
    except Exception as e:  # pylint: disable=broad-except
        hint = ("; an ESP32 node running MUI (displaymode COLOR) serves no network API"
                if isinstance(e, ConnectionRefusedError) else "")
        module.fail_json(msg=f"{p['host']}:{p['port']}: cannot read the node: "
                             f"{type(e).__name__}: {e}{hint}")
    result = {"changed": bool(changes), "written": summary(changes), "facts": facts,
              "api_closed": False}
    if changes:
        result["diff"] = diff
    refusal = config.managed_refusal(changes, facts)
    if refusal:
        module.fail_json(msg=f"{p['host']}: {refusal}", **result)
    try:
        steps = netconfig.phases(changes)
    except config.ConfigError as e:
        module.fail_json(msg=str(e), **result)
    if not changes or module.check_mode:
        module.exit_json(**result)

    gate.check(module)   # the layout's go-ahead, before the first write
    for phase in steps:
        try:
            write(p, phase)
        except Exception as e:  # pylint: disable=broad-except
            module.fail_json(msg=f"writing {summary(phase)}: {type(e).__name__}: {e}", **result)
        if netconfig.closes_api(phase):
            facts, left, why, result["api_closed"] = settle_closing(p, desired, phase)
            if result["api_closed"]:
                module.warn(f"{p['host']}: the node stopped answering on its API after the switch "
                            "to displaymode COLOR, as the firmware does under that UI; the switch "
                            "is not read back")
        else:
            facts, left, why = settle(p, desired, phase)
        if facts is not None:
            result["facts"] = facts
        if left:
            module.fail_json(msg="the node did not take the configuration: "
                                 + (why or f"still differs in {summary(left)}"), **result)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
