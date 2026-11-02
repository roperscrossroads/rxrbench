"""Stock Meshtastic over its network API (TCP, port 4403): the facts config.plan() compares.

A stock node has no state line, so what `meshtastic state` gives the port's firmware is read
here from the phone API itself, through the stock library's TCPInterface, and shaped the same
way (owner, device, channels with keys as fingerprints, the API-only LoRa and security fields,
`managed`). config.plan() and config.write() then work unchanged.

Beyond what config.py manages, a stock node's display config is managed here
(`displaymode` picks a firmware's UI on boards that carry two; `screen_on_secs`), because
they are only reachable through the phone API.

One display mode closes the route this module uses: under C(COLOR) (MUI, the touch/menu UI) the
stock ESP32 firmware starts neither the TCP API nor the web server (`WiFiAPClient.cpp`,
`onNetworkConnected`), so a switch to it is written last and by itself, and can only be
confirmed by the API going away. The way back to the button UI is on the device (its menus,
or a boot key on boards whose firmware has one), not over the network.

The radio (region, preset, power, transmit) is not managed here: on the bench that goes
through the RF rules, and a network route has no rule hook yet.
"""
from . import config, enums, state

DISPLAY_FIELDS = {"displaymode": ("DEFAULT", "TWOCOLOR", "INVERTED", "COLOR"),
                  "screen_on_secs": int}
PORT = 4403
API_OFF_MODES = ("COLOR",)


def connect(host, port=PORT, timeout=30, nodes=False):
    """A connected TCPInterface. Without `nodes` it skips the node database download
    (`noNodes`): only the node's own configuration and channels come across."""
    from meshtastic.tcp_interface import TCPInterface  # pylint: disable=import-outside-toplevel
    return TCPInterface(hostname=host, portNumber=port, noNodes=not nodes, timeout=timeout)


def channel_facts(ch):
    """A Channel protobuf as the state line would show it, or None for a disabled slot."""
    role = enums.CHANNEL_ROLE.get(int(ch.role), f"ROLE_{int(ch.role)}")
    if role == "DISABLED":
        return None
    psk = bytes(ch.settings.psk)
    if psk == b"":
        key = {"psk": "none"}
    elif len(psk) == 1:
        key = {"psk": "default" if psk[0] == 1 else f"default{psk[0]}", "expanded": False}
    elif psk == state.DEFAULT_PSK:
        key = {"psk": "default", "expanded": True}
    else:
        key = {"psk": "secret", "fingerprint": state.key_fingerprint(psk)}
    return {"index": int(ch.index), "name": ch.settings.name, "role": role,
            "uplink_enabled": bool(ch.settings.uplink_enabled),
            "downlink_enabled": bool(ch.settings.downlink_enabled), **key}


def facts_of(iface):
    """The facts of a connected node, in config.plan()'s terms, plus `display`."""
    from meshtastic.protobuf import config_pb2  # pylint: disable=import-outside-toplevel

    node = iface.localNode
    local = node.localConfig
    info = iface.getMyNodeInfo() or {}
    user = info.get("user") or {}
    meta = getattr(iface, "metadata", None)
    return {
        "node_id": user.get("id"),
        "firmware": getattr(meta, "firmware_version", None) or None,
        "owner": {"long_name": user.get("longName"), "short_name": user.get("shortName")},
        "device": {
            "role": enums.name(enums.DEVICE_ROLE, int(local.device.role), "ROLE"),
            "rebroadcast_mode": enums.name(enums.REBROADCAST_MODE,
                                           int(local.device.rebroadcast_mode), "REBROADCAST"),
        },
        "lora_api": {"hop_limit": int(local.lora.hop_limit)},
        "security_api": config.security_api(local.security),
        "managed": bool(local.security.is_managed),
        "channels": [c for c in (channel_facts(ch) for ch in node.channels or []) if c],
        "display": {
            "displaymode": config_pb2.Config.DisplayConfig.DisplayMode.Name(
                int(local.display.displaymode)),
            "screen_on_secs": int(local.display.screen_on_secs),
        },
    }


def check_display(display):
    for k, v in (display or {}).items():
        allowed = DISPLAY_FIELDS.get(k)
        if allowed is None:
            raise config.ConfigError(f"display.{k} is not managed here")
        if allowed is int:
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise config.ConfigError(f"display.{k} is a whole number of seconds, got {v!r}")
        elif v not in allowed:
            raise config.ConfigError(f"display.{k}: {v!r} is not one of {', '.join(allowed)}")


def plan(desired, facts):
    """config.plan(), and the display section beside it."""
    check_display(desired.get("display"))
    changes, diff = config.plan({k: v for k, v in desired.items() if k != "display"}, facts)
    want = {k: v for k, v in (desired.get("display") or {}).items() if v is not None}
    have = facts.get("display") or {}
    dd = {k: v for k, v in want.items() if have.get(k) != v}
    if dd:
        changes["display"] = dd
        diff["before"]["display"] = {k: have.get(k) for k in dd}
        diff["after"]["display"] = dd
    return changes, diff


def closes_api(changes):
    """Whether these changes switch the node to a display mode it serves no TCP API under."""
    return (changes.get("display") or {}).get("displaymode") in API_OFF_MODES


def phases(changes):
    """config.phases(), with a switch to a display mode that closes the API moved to the end, by
    itself: nothing can be written or read back after it. A node made managed in the same run is
    refused, as that write would have to come after it too."""
    if not closes_api(changes):
        return config.phases(changes)
    if (changes.get("security") or {}).get("is_managed"):
        raise config.ConfigError("display.displaymode COLOR closes the network API and "
                                 "security.is_managed must be written last: do them in two runs")
    rest = {k: v for k, v in changes.items() if k != "display"}
    display = dict(changes["display"])
    mode = display.pop("displaymode")
    if display:
        rest["display"] = display
    return config.phases(rest) + [{"display": {"displaymode": mode}}]
