#!/usr/bin/python
"""Bring a stock Meshtastic node to what is declared, over its own phone API (USB or network)."""

DOCUMENTATION = r"""
module: meshtastic_stock_config
short_description: Radio, owner, device, display, channels and admin keys of a stock Meshtastic node
description:
  - "Brings a node running stock Meshtastic to what its layout declares, through the stock
    C(meshtastic) library: over its USB port (C(transport=serial)) or its network API
    (C(transport=tcp), TCP 4403)."
  - "Over USB the port is resolved by name and checked (C(devreg resolve), C(devreg guard))
    each time it is opened, and locked (C(devlock)) for the whole run. Over the network the
    address must be the one the registry records for the route (C(devreg guard --route))."
  - "The order: the radio first, as one LoRa config message (region, preset, power,
    transmitter), checked against the RF rules before it is sent; then owner, device, LoRa
    hop limit, display and channels as one edit transaction; the security section last, by
    itself. Each step is read back until the node shows it, or the module fails after
    C(settle) seconds."
  - A node whose region is UNSET switches its transmitter on when it is given a region; when
    the layout declares C(tx_enabled=false), the transmitter is written off again at once.
  - Only what is declared is managed. A managed node (C(security.is_managed)) that differs is
    refused. Keys are never returned or shown; a diff shows a channel key as its fingerprint.
  - With nothing declared it only reads (C(facts)). Supports check mode and diff.
  - Zephyrtastic boards are configured by M(rxrbench.mesh.meshtastic_radio) and
    M(rxrbench.mesh.meshtastic_config), through their shell; this module is for stock firmware.
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
  registry_name:
    description: The board's name in the device registry. Default is C(board).
    type: str
  transport:
    description: C(serial), the node's USB port; or C(tcp), its network API.
    type: str
    choices: [serial, tcp]
    default: serial
  host:
    description: (tcp) The node's address, as the registry records it for the route.
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
    description: The device registry tool on this host.
    type: path
    default: ~/.devreg/devreg.py
  devlock_dir:
    description: (serial) Where devlock.py lives on this host.
    type: path
    default: ~/.devreg
  radio:
    description: The radio, as C(meshtastic.config.lora) declares it.
    type: dict
    suboptions:
      region: {type: str, description: "The region by its Meshtastic name (C(US), C(EU_868), ...)."}
      modem_preset: {type: str, description: "The preset by its Meshtastic name (C(LONG_FAST), ...)."}
      use_preset: {type: bool, description: Only true is accepted (a custom modem is not managed).}
      tx_power: {type: int, description: "dBm, 0 to 30; 0 is the region's default."}
      tx_enabled: {type: bool, description: Whether the node may transmit.}
  rf_rules:
    description: The RF rules the layout resolved to (C(bench_rf_rules)). The radio block is
      checked against them before it is written; absent, the built-in rules.
    type: dict
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
    description: Only C(hop_limit) (1 to 7); the radio is C(radio).
    type: dict
    suboptions:
      hop_limit: {type: int, description: The hop limit of packets this node originates.}
  display:
    description: As in M(rxrbench.mesh.meshtastic_net_config).
    type: dict
    suboptions:
      displaymode: {type: str, choices: [DEFAULT, TWOCOLOR, INVERTED, COLOR], description: The UI.}
      screen_on_secs: {type: int, description: Seconds the screen stays on.}
  security:
    description: As in M(rxrbench.mesh.meshtastic_config).
    type: dict
    suboptions:
      admin_key: {type: list, elements: str, description: "Admin public keys, C(base64:) or C(hex:), at most 3."}
      is_managed: {type: bool, description: Whether the node takes configuration only from a remote admin.}
  channels:
    description: The channels to manage.
    type: list
    elements: dict
    suboptions:
      index: {type: int, required: true, description: The slot.}
      role: {type: str, required: true, choices: [PRIMARY, SECONDARY, DISABLED], description: The role.}
      name: {type: str, description: The channel name.}
      psk: {type: str, default: default, description: "C(default), C(none), C(base64:<key>) or C(hex:<key>)."}
      uplink_enabled: {type: bool, description: MQTT uplink. Left alone if absent.}
      downlink_enabled: {type: bool, description: MQTT downlink. Left alone if absent.}
  connect_timeout:
    description: Seconds one connection and its configuration download may take.
    type: int
    default: 30
  open_deadline:
    description: Seconds to keep trying to open the node (a reset on open, a restart).
    type: float
    default: 90.0
  flush:
    description: Seconds to hold the connection open after a write, so the node has taken
      every message before the connection goes away.
    type: float
    default: 6.0
  settle:
    description: Seconds the node has to show the declared state after a write.
    type: float
    default: 120.0
  api_off_grace:
    description: After a switch to C(displaymode=COLOR), seconds the node must stay unanswered
      before the switch is taken as done (stock ESP32 firmware serves no network API under it).
    type: float
    default: 45.0
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: A stock node on USB, on the bench's preset at low power, with its channels
  rxrbench.mesh.meshtastic_stock_config:
    board: heltec1
    radio: {region: US, modem_preset: SHORT_TURBO, tx_power: 2, tx_enabled: true}
    owner: {long_name: bench heltec1, short_name: h1}
    channels:
      - {index: 0, role: PRIMARY, psk: default}
"""

RETURN = r"""
written:
  description: What was (or, in check mode, would be) written, by section. Channels by index.
  returned: always
  type: dict
  sample: {radio: [modem_preset, tx_power], channels: [0]}
facts:
  description: What the node showed last (node id, firmware, radio, owner, device, display,
    channels with keys as fingerprints, the hop limit, the admin keys, managed).
  returned: always
  type: dict
api_closed:
  description: The node stopped answering after a switch to C(COLOR); that switch was not read back.
  returned: always
  type: bool
"""

import time

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.mesh.plugins.module_utils import (
    config, meshrf, netconfig, stocknode, stockradio)

try:
    import meshtastic  # noqa: F401 pylint: disable=unused-import
    HAS_MESHTASTIC = True
except ImportError:
    HAS_MESHTASTIC = False

SECTIONS = ("owner", "device", "lora", "display", "security")


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


def plan(desired, facts):
    """(changes, diff): the radio (stockradio) and the rest (netconfig), as one change set."""
    changes, diff = netconfig.plan({k: v for k, v in desired.items() if k != "radio"}, facts)
    rc, rd = stockradio.plan(desired.get("radio") or {}, facts.get("radio") or {})
    if rc:
        changes = {"radio": rc, **changes}
        diff["before"]["radio"], diff["after"]["radio"] = rd["before"], rd["after"]
    return changes, diff


def phases(changes):
    """The radio by itself first; then netconfig's order (everything else, then security; a
    switch that closes the API last)."""
    rest = {k: v for k, v in changes.items() if k != "radio"}
    return ([{"radio": changes["radio"]}] if "radio" in changes else []) + \
        (netconfig.phases(rest) if rest else [])


def write(session, phase, flush, sleep=time.sleep):
    iface = session.open()
    try:
        if "radio" in phase:
            stockradio.write(iface, phase["radio"])
        else:
            config.write(iface, phase)
        sleep(flush)
    finally:
        iface.close()


def left_of(desired, facts, phase):
    """What of `phase` the node does not show yet; of the radio, only the fields it wrote."""
    left = {k: v for k, v in plan(desired, facts)[0].items() if k in phase}
    if "radio" in left:
        radio = {f: v for f, v in left["radio"].items() if f in phase["radio"]}
        if radio:
            left["radio"] = radio
        else:
            del left["radio"]
    return left


def first_wait(desired, before, phase):
    """The part of a radio phase to wait for before stockradio.followup(): without the
    transmitter, when a region given to an UNSET node will switch it on against the layout."""
    radio = phase.get("radio")
    if (radio and "region" in radio and before.get("radio", {}).get("region") == "UNSET"
            and desired["radio"].get("tx_enabled") is False):
        rest = {k: v for k, v in radio.items() if k != "tx_enabled"}
        return {"radio": rest} if rest else phase
    return phase


def settle(session, desired, phase, p, sleep=time.sleep, clock=time.monotonic):
    """Read the node until it shows `phase`. Returns (facts, what still differs, last error)."""
    end, facts, left, why = clock() + p["settle"], None, phase, None
    sleep(3)
    while clock() < end:
        try:
            facts = session.read(min(p["open_deadline"], max(end - clock(), 1.0)))
            left, why = left_of(desired, facts, phase), None
            if not left:
                break
        except Exception as e:  # pylint: disable=broad-except
            why = f"{type(e).__name__}: {e}"
        sleep(3)
    return facts, left, why


def settle_closing(session, desired, phase, p, sleep=time.sleep, clock=time.monotonic):
    """settle() for a switch to a display mode the node may serve no API under: it either stays
    away for `api_off_grace` seconds (taken as done) or comes back and is compared."""
    end, facts, left, why, down_since = clock() + p["settle"], None, phase, None, None
    sleep(3)
    while clock() < end:
        try:
            facts = session.read(min(p["connect_timeout"], max(end - clock(), 1.0)))
            down_since = None
            left, why = left_of(desired, facts, phase), None
            if not left:
                return facts, {}, None, False
        except Exception as e:  # pylint: disable=broad-except
            why = f"{type(e).__name__}: {e}"
            down_since = clock() if down_since is None else down_since
            if clock() - down_since >= p["api_off_grace"]:
                return facts, {}, None, True
        sleep(3)
    return facts, left, why, False


def desired_of(p):
    desired = {sec: {k: v for k, v in (p[sec] or {}).items() if v is not None} for sec in SECTIONS}
    desired["channels"] = [{k: v for k, v in ch.items() if v is not None}
                           for ch in p["channels"] or []]
    desired["radio"] = {k: v for k, v in (p["radio"] or {}).items() if v is not None}
    return desired


def session_of(p):
    return stocknode.Session(p["transport"], p["board"], device=p["registry_name"],
                             host=p["host"], port=p["port"], bearer=p["bearer"],
                             devreg=p["devreg"], devlock_dir=p["devlock_dir"],
                             timeout=p["connect_timeout"])


def run(module, p, session):
    desired = desired_of(p)
    try:
        stockradio.check(desired["radio"])
    except stockradio.RadioError as e:
        module.fail_json(msg=f"{p['board']}: {e}")
    with session:
        try:
            facts = session.read(p["open_deadline"])
            changes, diff = plan(desired, facts)
        except stocknode.Unanswered:
            raise
        except (config.ConfigError, stockradio.RadioError) as e:
            module.fail_json(msg=f"{p['board']}: {e}")
        except Exception as e:  # pylint: disable=broad-except
            module.fail_json(msg=f"{p['board']}: cannot read the node: {type(e).__name__}: {e}")
        result = {"changed": bool(changes), "written": summary(changes), "facts": facts,
                  "api_closed": False}
        if changes:
            result["diff"] = diff
        if "radio" in changes:
            refused = meshrf.check_write("meshtastic", p["board"], desired["radio"],
                                         p["rf_rules"], tx_now=facts["radio"].get("tx_enabled"))
            if refused:
                module.fail_json(msg="the radio breaks the RF rules: " + "; ".join(refused),
                                 **result)
        refusal = config.managed_refusal(changes, facts)
        if refusal:
            module.fail_json(msg=f"{p['board']}: {refusal}", **result)
        try:
            steps = phases(changes)
        except config.ConfigError as e:
            module.fail_json(msg=str(e), **result)
        if not changes or module.check_mode:
            module.exit_json(**result)

        gate.check(module)   # the layout's go-ahead, before the first write
        for phase in steps:
            before = facts
            try:
                write(session, phase, p["flush"])
            except Exception as e:  # pylint: disable=broad-except
                module.fail_json(msg=f"writing {summary(phase)}: {type(e).__name__}: {e}", **result)
            if netconfig.closes_api(phase):
                facts, left, why, result["api_closed"] = settle_closing(session, desired, phase, p)
                if result["api_closed"]:
                    module.warn(f"{p['board']}: the node stopped answering after the switch to "
                                "displaymode COLOR; the switch is not read back")
            else:
                facts, left, why = settle(session, desired, first_wait(desired, before, phase), p)
            if facts is not None and "radio" in phase:
                again = stockradio.followup(desired["radio"], before["radio"], facts["radio"])
                if again:
                    module.warn(f"{p['board']}: the firmware switched the transmitter on with "
                                "the region; it is switched off again")
                    write(session, {"radio": again}, p["flush"])
                    facts, left, why = settle(session, desired, phase, p)
            if facts is not None:
                result["facts"] = facts
            if left:
                module.fail_json(msg="the node did not take the configuration: "
                                     + (why or f"still differs in {summary(left)}"), **result)
        module.exit_json(**result)


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "board": {"type": "str", "required": True},
            "registry_name": {"type": "str"},
            "transport": {"type": "str", "choices": ["serial", "tcp"], "default": "serial"},
            "host": {"type": "str"},
            "port": {"type": "int", "default": netconfig.PORT},
            "bearer": {"type": "str", "default": "wifi"},
            "devreg": {"type": "path", "default": "~/.devreg/devreg.py"},
            "devlock_dir": {"type": "path", "default": "~/.devreg"},
            "radio": {"type": "dict", "options": {
                "region": {"type": "str"}, "modem_preset": {"type": "str"},
                "use_preset": {"type": "bool"}, "tx_power": {"type": "int"},
                "tx_enabled": {"type": "bool"}}},
            "rf_rules": {"type": "dict"},
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
            "open_deadline": {"type": "float", "default": 90.0},
            "flush": {"type": "float", "default": 6.0},
            "settle": {"type": "float", "default": 120.0},
            "api_off_grace": {"type": "float", "default": 45.0},
        },
        required_if=[("transport", "tcp", ("host",))],
        supports_check_mode=True,
    )
    p = module.params
    if not HAS_MESHTASTIC:
        module.fail_json(msg="the `meshtastic` Python library is not installed for this "
                             "module's interpreter; it is how a stock node is read")
    try:
        run(module, p, session_of(p))
    except stocknode.NodeError as e:
        # `unanswered`: the node is not there to read (the play skips it, not the bench)
        module.fail_json(msg=str(e), unanswered=isinstance(e, stocknode.Unanswered))


if __name__ == "__main__":
    main()
