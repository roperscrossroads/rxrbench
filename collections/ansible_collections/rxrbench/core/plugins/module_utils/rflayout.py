"""The RF rules against a layout: every board's declared radio, asked of its family's plugin.

Only what is declared is checked. A board whose family's radio this tool sets and whose
layout declares none is reported, not passed silently. Runs on the controller (the families'
plugins are asked); rfrules.py holds what a module on a bench host shares with it.
"""
from . import rfrules
from ..plugin_utils import families


defaults, ceiling, tighten = rfrules.defaults, rfrules.ceiling, rfrules.tighten


def _declared(name, hostvars):
    fam = families.declared(hostvars)
    return families.plugin(fam).rf_declared(name, hostvars, families.radio(fam) == "managed")


def _left_to_firmware(hostvars):
    """A firmware whose radio this tool does not set unless told to (`unmanaged`), with no
    radio declared: it transmits at its own defaults. One whose layout declares its radio has
    it written and checked like any other."""
    fam = families.declared(hostvars)
    return bool(fam) and families.radio(fam) == "unmanaged" \
        and _declared("", hostvars)["transmits"] is None


def transmits(hostvars):
    """Does this board's declared state let it key up? None when nothing is declared. A
    firmware left at its own radio defaults counts as transmitting."""
    if _left_to_firmware(hostvars):
        return True
    return _declared("", hostvars)["transmits"]


def check_board(name, hostvars, rules=None):
    """Every violation for one board: what its family says is wrong with the declaration,
    the ceiling against what it would emit, then the family's own rules."""
    rules = rfrules.with_region({**defaults(), **(rules or {})})
    declared = _declared(name, hostvars)
    out = list(declared["problems"])
    if declared["transmits"]:
        out += rfrules.check_emissions(name, declared["emissions"], rules)
    return out + families.plugin(families.declared(hostvars)).rf_check(name, hostvars, rules)


def check_layout(all_hostvars, rules=None):
    """all_hostvars: {board: hostvars}. Every violation, by board, across the layout."""
    out = []
    for name in sorted(all_hostvars):
        out.extend(check_board(name, all_hostvars[name], rules))
    return out


def warnings(all_hostvars):
    """Boards whose RF behaviour these rules cannot govern."""
    out = []
    for name in sorted(all_hostvars):
        fam = families.declared(all_hostvars[name])
        if _left_to_firmware(all_hostvars[name]):
            out.append(f"{name}: runs {fam} firmware, whose power and frequency this tool does "
                       f"not set: it transmits at that firmware's own defaults")
    return out


def boot_radio(name, image, rules=None):
    """Problems with what an image transmits before its config is written (the catalog's
    `boot_radio`: [{freq_mhz, bw_khz, tx_dbm, radio}], a firmware's build defaults), judged
    against the rules like a declaration. [] when the catalog records none."""
    radios = (image or {}).get("boot_radio") or []
    if isinstance(radios, dict):
        radios = [radios]
    rules = rfrules.with_region({**defaults(), **(rules or {})})
    emissions = [{"radio": r.get("radio", "lora"), "key": "its boot tx_dbm",
                  "power_dbm": r.get("tx_dbm"), "freq_mhz": r.get("freq_mhz"),
                  "bw_khz": r.get("bw_khz")} for r in radios]
    # said of the board already (in a warning, by its flasher): without its name in front
    return [p[len(name) + 2:] if p.startswith(f"{name}: ") else p
            for p in rfrules.check_emissions(name, emissions, rules)]
