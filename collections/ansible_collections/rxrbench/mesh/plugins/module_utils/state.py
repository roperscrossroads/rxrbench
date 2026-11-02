"""A board's actual state, as one dict whatever firmware it runs.

New firmware answers `meshtastic state` with one machine-readable line. Firmware older than
that only prints prose; `legacy_facts()` reads the few lines of it that are stable enough to
trust (version, class, node id, preset, power, TX, role) and says `state_supported: False`, so
a caller knows everything else is unknown rather than absent.

Names follow the stock Meshtastic schema (`SHORT_TURBO`, `CLIENT`, `ALL`), so actual state
compares directly with a layout's declared state.
"""
import binascii
import re

from . import consoles, enums  # noqa: F401  pylint: disable=unused-import  (consoles registers)
from ansible_collections.rxrbench.core.plugins.module_utils.stateline import STATE_LINE, StateError, parse_line  # noqa: F401  pylint: disable=unused-import

LEGACY_COMMANDS = ("meshtastic version", "meshtastic status", "meshtastic lora",
                   "meshtastic preset")

# The firmware's display names (meshtastic_preset_display_name) -> the stock enum names.
PRESETS = {
    "ShortTurbo": "SHORT_TURBO", "ShortFast": "SHORT_FAST", "ShortSlow": "SHORT_SLOW",
    "MediumFast": "MEDIUM_FAST", "MediumSlow": "MEDIUM_SLOW", "MediumTurbo": "MEDIUM_TURBO",
    "LongFast": "LONG_FAST", "LongSlow": "LONG_SLOW", "LongMod": "LONG_MODERATE",
    "LongTurbo": "LONG_TURBO", "LiteFast": "LITE_FAST", "LiteSlow": "LITE_SLOW",
    "NarrowFast": "NARROW_FAST", "NarrowSlow": "NARROW_SLOW", "TinyFast": "TINY_FAST",
    "TinySlow": "TINY_SLOW",
}
PRESET_DISPLAY = {v: k for k, v in PRESETS.items()}

REGIONS = enums.REGION

STATE_COMMAND = "meshtastic state"
STATE_VERSION = 1


def _one(pattern, text, what, cast=str, required=True):
    m = re.search(pattern, text, re.M)
    if not m:
        if required:
            raise StateError(f"cannot read {what}")
        return None
    return cast(m.group(1))


def _preset(name, what):
    if name not in PRESETS:
        raise StateError(f"{what}: unknown preset name {name!r}")
    return PRESETS[name]


def version_facts(output):
    """Build, board, image and class from `meshtastic version`: every firmware has it, a
    keyless head included. Enough to say what a board runs when it has no state line."""
    image = _one(r"^image:\s+(\d+\.\d+\.\d+\+\d+) ", output, "the image version", required=False)
    return {"state_supported": False,
            "build": _one(r"^build:\s+(\S+)", output, "the build id"),
            "board": _one(r"^board:\s+(\S+)", output, "the board"),
            "image": image,
            "class": _one(r"^class:\s+(\d+)", output, "the fleet class", int)}


def legacy_facts(outputs):
    """outputs: {command: its output}, for LEGACY_COMMANDS. Returns the normalised facts.

    Strict: a line that should be there and is not raises, because a fact that silently reads
    as absent is how a truncated read gets mistaken for a healthy board.
    """
    missing = [c for c in LEGACY_COMMANDS if c not in outputs]
    if missing:
        raise StateError(f"missing output of: {missing}")
    ver, status = outputs["meshtastic version"], outputs["meshtastic status"]
    lora, preset = outputs["meshtastic lora"], outputs["meshtastic preset"]
    image = _one(r"^image:\s+(\d+\.\d+\.\d+\+\d+) ", ver, "the image version", required=False)
    region = _one(r"^region:\s+(-?\d+)\s*$", lora, "the region", int)
    return {
        "state_supported": False,
        "build": _one(r"^build:\s+(\S+)", ver, "the build id"),
        "built": _one(r"^build:\s+\S+ \(built ([0-9-]+) UTC\)", ver, "the build time",
                      required=False),
        "board": _one(r"^board:\s+(\S+)", ver, "the board"),
        "image": image,
        "class": _one(r"^class:\s+(\d+)", ver, "the fleet class", int),
        "node_id": _one(r"^node: (0x[0-9a-f]{8})\s*$", status, "the node id"),
        "lora": {
            "modem_preset": _preset(_one(r"^modem preset: (\S+)", lora, "the stored preset"),
                                    "stored preset"),
            "live_preset": _preset(_one(r"^preset\s+(\S+) \(generation \d+\)", preset,
                                        "the live preset"), "live preset"),
            "region": REGIONS.get(region, f"REGION_{region}"),
            "tx_power": _one(r"^tx power:\s+(-?\d+) dBm at the antenna", lora, "the TX power", int),
            "tx_power_is_region_default": "[region default]" in lora,
            "tx_enabled": _one(r"^tx enabled:\s+(yes|NO)", lora, "TX enabled") == "yes",
        },
        "device": {
            "role": _one(r"^device role: (\S+)", status, "the device role").upper(),
            "rebroadcast_mode": _one(r"^rebroadcast mode: (\S+)", status,
                                     "the rebroadcast mode").upper(),
        },
    }


# The well-known default channel key, which the one-byte form `1` stands for. It is public
# (it is in every Meshtastic client), so naming it here discloses nothing.
DEFAULT_PSK = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
KFP_LABEL = b"zt-kfp-1"


def key_fingerprint(key):
    """The fingerprint the firmware prints for a key: CRC-32 over a label and the key."""
    return f"{binascii.crc32(KFP_LABEL + bytes(key)) & 0xFFFFFFFF:08x}"


def key_kind(k):
    """A channel key as the state line names it -> what a layout calls it.

    `default` is the well-known key in either form; `expanded` says it is stored as its 16
    bytes, which is the same key on the air but shows a lock in the client apps.
    """
    if k == "none":
        return {"psk": "none"}
    if k == "s1":
        return {"psk": "default", "expanded": False}
    if k.startswith("s"):
        return {"psk": f"default{int(k[1:])}", "expanded": False}
    if k[2:] == key_fingerprint(DEFAULT_PSK):
        return {"psk": "default", "expanded": True}
    return {"psk": "secret", "fingerprint": k[2:]}


def _head_preset(n):
    return None if n < 0 else enums.name(enums.MODEM_PRESET, n, "PRESET")


def state_facts(output):
    """The facts in the output of `meshtastic state`, or None when the firmware has no such
    command. A line that is there but damaged raises: it must not be read as "no state"."""
    j = parse_line(output)
    if j is None:
        return None
    if j.get("v") != STATE_VERSION:
        raise StateError(f"state version {j.get('v')!r}, this tool reads {STATE_VERSION}")
    lora, dev = j.get("lora") or {}, j.get("dev") or {}
    facts = {
        "state_supported": True,
        "build": j["build"], "built": None, "board": j["board"], "image": j.get("image"),
        "class": j["class"], "node_id": j["id"], "features": j.get("feat", []),
        "owner": {"long_name": (j.get("own") or {}).get("l"),
                  "short_name": (j.get("own") or {}).get("s")},
        "lora": {
            "modem_preset": enums.name(enums.MODEM_PRESET, lora.get("preset"), "PRESET"),
            "use_preset": bool(lora.get("use_preset", 1)),
            "live_preset": enums.name(enums.MODEM_PRESET, lora.get("live"), "PRESET"),
            "region": enums.name(enums.REGION, lora.get("region"), "REGION"),
            "tx_power": lora.get("pwr"),
            "tx_power_is_region_default": lora.get("pwr") == 0,
            "tx_enabled": bool(lora.get("tx")),
        },
        "device": {
            "role": enums.name(enums.DEVICE_ROLE, dev.get("role"), "ROLE"),
            "rebroadcast_mode": enums.name(enums.REBROADCAST_MODE, dev.get("rb"), "REBROADCAST"),
        },
        "channels": [
            {"index": c["i"], "name": c["n"], "hash": c["h"],
             "role": enums.name(enums.CHANNEL_ROLE, c["r"], "ROLE"),
             "uplink_enabled": bool(c["up"]), "downlink_enabled": bool(c["dn"]),
             **key_kind(c["k"])}
            for c in j.get("ch", [])
        ],
        "managed": bool(j["managed"]) if "managed" in j else None,
    }
    if "brain" in j:
        facts["brain"] = {
            "max_heads": j["brain"]["max"],
            "heads": [{"attachment": h["a"], "node_id": h["node"], "link_up": bool(h["up"]),
                       "preset": _head_preset(h["p"]),
                       # What the brain stores for the head and re-sends when the head
                       # reports another (firmware 0.4.39 on; older firmware has no "w").
                       "wanted_preset": _head_preset(h.get("w", -1))}
                      for h in j["brain"]["heads"]],
        }
    if "head" in j:
        facts["head"] = {"brain": j["head"]["brain"]}
    if "scan" in j:
        facts["scanner"] = {
            "tx_gate_shut": bool(j["scan"]["shut"]), "sweeping": bool(j["scan"]["sweep"]),
            "presets": [enums.name(enums.MODEM_PRESET, p, "PRESET") for p in j["scan"]["presets"]],
        }
    if "cluster" in j:
        c = j["cluster"]
        facts["cluster"] = {"channel_index": None if c["ch"] < 0 else c["ch"],
                            "entries": c["n"], "doc_hash": c["hash"]}
    return facts


def read(board, deadline=8.0, command=None, attempts=3):
    """A board's state facts through its broker (None: the firmware prints no state line). A
    line damaged on the way (cut short by the console, a bad checksum) is read again: it says
    nothing about the board, and a plan must not fail on it."""
    command = command or STATE_COMMAND
    for i in range(attempts):
        try:
            return state_facts(board.exec(command, deadline=deadline))
        except StateError:
            if i == attempts - 1:
                raise
    return None

