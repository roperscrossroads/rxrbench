"""The radio config a board must have before anything else: preset, power, transmitter.

These go over the plain shell, not the phone API: they work on a keyless head, and they are
the first thing done to a freshly flashed board, which may be on the public preset at full
power. `plan()` is pure: declared and actual state in, shell commands and a diff out.

The order is the point. While the preset or the power is wrong the transmitter is switched
off first, the power is set before the preset, and the transmitter goes back on last. A
board never keys up on a preset or at a power the layout did not declare, not even for the
moment between two commands.
"""
from . import state

FIELDS = ("modem_preset", "tx_power", "tx_enabled")
# The presets the firmware's `meshtastic lora preset` takes by name.
SETTABLE = ("LONG_FAST", "LONG_SLOW", "LONG_MODERATE", "MEDIUM_SLOW", "MEDIUM_FAST",
            "SHORT_SLOW", "SHORT_FAST", "SHORT_TURBO")


class RadioError(ValueError):
    pass


def check(desired):
    """Refuse what the shell cannot be asked for, before anything is sent."""
    unknown = sorted(set(desired) - set(FIELDS) - {"region", "use_preset"})
    if unknown:
        raise RadioError(f"meshtastic_radio does not manage: {', '.join(unknown)}")
    preset = desired.get("modem_preset")
    if preset is not None and preset not in SETTABLE:
        raise RadioError(f"preset {preset!r} cannot be set from the shell "
                         f"(one of {', '.join(SETTABLE)})")
    power = desired.get("tx_power")
    if power is not None and (isinstance(power, bool) or not isinstance(power, int)
                              or not 0 <= power <= 30):
        raise RadioError(f"tx_power must be an integer from 0 to 30, got {power!r}")
    tx = desired.get("tx_enabled")
    if tx is not None and not isinstance(tx, bool):
        raise RadioError(f"tx_enabled must be true or false, got {tx!r}")


def diff(desired, actual):
    """{field: (actual, desired)} for every declared field that differs. A field the layout
    does not declare is not managed and never differs."""
    return {f: (actual.get(f), desired[f]) for f in FIELDS
            if desired.get(f) is not None and actual.get(f) != desired[f]}


def plan(desired, actual):
    """The shell commands that take the board from `actual` to `desired`, in a safe order."""
    check(desired)
    if actual.get("use_preset") is False and desired.get("modem_preset") is not None:
        raise RadioError("the board uses a custom modem (use_preset off); setting a preset "
                         "would replace it")
    d = diff(desired, actual)
    cmds = []
    retune = "modem_preset" in d or "tx_power" in d
    tx_now = bool(actual.get("tx_enabled"))
    if retune and tx_now:
        cmds.append("meshtastic lora tx off")
        tx_now = False
    if "tx_power" in d:
        cmds.append(f"meshtastic lora power {desired['tx_power']}")
    if "modem_preset" in d:
        cmds.append(f"meshtastic lora preset {state.PRESET_DISPLAY[desired['modem_preset']]}")
    # Where the transmitter ends up: as declared, or as it was if the layout does not say.
    tx_want = desired.get("tx_enabled")
    if tx_want is None:
        tx_want = bool(actual.get("tx_enabled"))
    if tx_want != tx_now:
        cmds.append(f"meshtastic lora tx {'on' if tx_want else 'off'}")
    return cmds, d
