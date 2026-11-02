"""Stock Meshtastic's radio over its phone API: region, preset, power and the transmitter.

The port's shell takes these one command at a time, so radio.py orders its commands
(transmitter off first, on last). The phone API takes the whole LoRa config in one message,
and the firmware applies it as one (it reconfigures the radio from the new config, or restarts
with it), so a stock node never runs on half of a change: one write carries every declared
field.

One firmware rule needs a second write. A node whose region is UNSET switches its transmitter
on when it is given a region, whatever the message says (the stock AdminModule). `followup()`
finds that, and the module writes the transmitter off again straight away. The node may key up
in between (it announces itself on a region change).

The values are the firmware's own enum numbers (enums.py), written as numbers, so a library
whose protobufs are older than the firmware still writes a newer preset or region.

`check()`, `facts_of()`, `plan()` and `followup()` are pure; `write()` takes a connected
stock-library interface.
"""
from . import enums

FIELDS = ("region", "modem_preset", "tx_power", "tx_enabled")
PRESETS = {v: k for k, v in enums.MODEM_PRESET.items()}
REGIONS = {v: k for k, v in enums.REGION.items()}


class RadioError(ValueError):
    pass


def check(desired):
    """Refuse what this module does not manage, or a value the firmware has no name for."""
    unknown = sorted(set(desired) - set(FIELDS) - {"use_preset"})
    if unknown:
        raise RadioError(f"the stock radio step does not manage: {', '.join(unknown)} "
                         "(hop_limit belongs in meshtastic.config.lora_api)")
    if desired.get("use_preset") is False:
        raise RadioError("use_preset: false (a custom modem) is not managed here")
    region = desired.get("region")
    if region is not None and region not in REGIONS:
        raise RadioError(f"region {region!r} is not a Meshtastic region")
    if region == "UNSET":
        raise RadioError("region UNSET switches the transmitter off for good: declare "
                         "tx_enabled: false instead")
    preset = desired.get("modem_preset")
    if preset is not None and preset not in PRESETS:
        raise RadioError(f"modem_preset {preset!r} is not a Meshtastic preset")
    power = desired.get("tx_power")
    if power is not None and (isinstance(power, bool) or not isinstance(power, int)
                              or not 0 <= power <= 30):
        raise RadioError(f"tx_power must be an integer from 0 to 30 (0: the region's "
                         f"default), got {power!r}")
    tx = desired.get("tx_enabled")
    if tx is not None and not isinstance(tx, bool):
        raise RadioError(f"tx_enabled must be true or false, got {tx!r}")


def facts_of(lora):
    """A LoRaConfig protobuf as names: what plan() compares and the facts show."""
    return {
        "region": enums.name(enums.REGION, int(lora.region), "REGION"),
        "modem_preset": enums.name(enums.MODEM_PRESET, int(lora.modem_preset), "PRESET"),
        "use_preset": bool(lora.use_preset),
        "tx_power": int(lora.tx_power),
        "tx_enabled": bool(lora.tx_enabled),
    }


def plan(desired, actual):
    """(change, diff): the declared fields that differ, as {field: value}. A field the layout
    does not declare is left as the node has it."""
    check(desired)
    if actual.get("use_preset") is False and desired.get("modem_preset") is not None:
        raise RadioError("the node uses a custom modem (use_preset off); setting a preset "
                         "would replace it")
    change = {f: desired[f] for f in FIELDS
              if desired.get(f) is not None and actual.get(f) != desired[f]}
    diff = {"before": {f: actual.get(f) for f in change}, "after": dict(change)}
    return change, diff


def followup(desired, before, after):
    """What must be written again after a write: the transmitter, when the firmware switched it
    on because the region left UNSET while the layout declares it off."""
    if (before.get("region") == "UNSET" and after.get("region") != "UNSET"
            and desired.get("tx_enabled") is False and after.get("tx_enabled")):
        return {"tx_enabled": False}
    return {}


def write(iface, change):
    """One LoRa config message with every changed field, the rest as the node has them."""
    node = iface.localNode
    cfg = node.localConfig.lora
    for k, v in change.items():
        if k == "region":
            cfg.region = REGIONS[v]
        elif k == "modem_preset":
            cfg.modem_preset = PRESETS[v]
            cfg.use_preset = True
        elif k == "tx_power":
            cfg.tx_power = int(v)
        elif k == "tx_enabled":
            cfg.tx_enabled = bool(v)
    node.writeConfig("lora")
