"""The RF rules for the two mesh vocabularies: what a layout's radio block declares, and
the check a radio module makes on the block in its hand before it writes.

The layout is checked when it loads, from the files it is made of. A value that reaches a play
another way (`-e`, a var file the inventory did not read) was never seen by that check, so the
module that writes a radio checks its block too, with the rules the layout resolved to
(`bench_rf_rules`; absent, the built-ins).
"""
from ansible_collections.rxrbench.core.plugins.module_utils import rfrules
from . import meshcore

DEFAULT_RULES = rfrules.defaults()
#: What a region allows a LoRa transmitter: the band its whole channel must sit in and the
#: most it may put out, in dBm. The values are the ones Meshtastic's firmware ships for the
#: region (its region table: US, EU_868, JP).
LORA_REGIONS = {
    "US": {"band_mhz": [902.0, 928.0], "max_tx_power": 30},
    "EU": {"band_mhz": [869.4, 869.65], "max_tx_power": 27},
    "JP": {"band_mhz": [920.5, 923.5], "max_tx_power": 13},
}
rfrules.register_radio("lora", LORA_REGIONS)
#: Meshtastic's names for those regions; a name not here is passed on as it is, and is then
#: no site's region.
REGION_CODES = {"US": "US", "EU_868": "EU", "JP": "JP"}

_NOTHING = {"transmits": None, "emissions": [], "problems": []}

#: The channel width of each Meshtastic modem preset the firmware can be set to, in kHz, in a
#: sub-GHz region (upstream Meshtastic, src/mesh/MeshRadio.h `modemPresetToParams`; the 2.4 GHz
#: region's wide LoRa is not one this bench uses). A preset says no frequency (the firmware
#: takes it from the region and the channel's name), but its width alone must fit the band.
PRESET_BW_KHZ = {"LONG_FAST": 250.0, "LONG_SLOW": 125.0, "LONG_MODERATE": 125.0,
                 "MEDIUM_SLOW": 250.0, "MEDIUM_FAST": 250.0, "SHORT_SLOW": 250.0,
                 "SHORT_FAST": 250.0, "SHORT_TURBO": 500.0}


def preset_problems(name, preset, rules, what="its modem_preset"):
    """Problems with a modem preset a board is declared to transmit on: a preset the firmware
    has no width for, or one whose channel is wider than the band the rules allow (the site's,
    where it states one, else the region's for LoRa). A narrow band (EU 869.4-869.65 MHz, 250
    kHz) takes no 500 kHz preset wherever the firmware puts the channel."""
    if preset is None:
        return []
    bw = PRESET_BW_KHZ.get(str(preset))
    if bw is None:
        return [f"{name}: {what} {preset} is not a preset this tool knows the width of "
                f"({', '.join(PRESET_BW_KHZ)})"]
    band = rules.get("band_mhz") or rfrules.limits(rules.get("region"), "lora").get("band_mhz")
    if band and bw / 1000.0 > band[1] - band[0]:
        return [f"{name}: {what} {preset} is {bw:g} kHz wide, wider than the band "
                f"{band[0]}-{band[1]} MHz"]
    return []


def meshtastic_rf_check(name, hostvars, rules, own=True):
    """What a Meshtastic radio block makes a board transmit that the ceiling's power check does
    not see: the width of its preset (`own`: unless the board's preset is set by another)."""
    lora = lora_of(hostvars) or {}
    if not own or not lora.get("tx_enabled", True):
        return []
    return preset_problems(name, lora.get("modem_preset"), rules)


def lora_of(hostvars):
    return ((hostvars.get("meshtastic") or {}).get("config") or {}).get("lora")


def meshtastic_declared(name, hostvars, managed):
    lora = lora_of(hostvars)
    if lora is None:
        return {**_NOTHING, "problems": [
            f"{name}: no meshtastic.config.lora declared: its preset and power are "
            f"unmanaged, and a fresh node boots on LongFast at full power"] if managed else []}
    if not lora.get("tx_enabled", True):
        return {**_NOTHING, "transmits": False}
    return {"transmits": True,
            "emissions": [{"radio": "lora", "key": "tx_power", "power_dbm": lora.get("tx_power"),
                           "region": REGION_CODES.get(lora.get("region"), lora.get("region"))}],
            "problems": [] if lora.get("modem_preset") is not None
            else [f"{name}: transmits but declares no modem_preset"]}


def meshcore_declared(name, hostvars, managed):
    block = hostvars.get("meshcore") or {}
    try:
        radio = None if block.get("radio") is None else meshcore.declared_radio(block["radio"])
    except meshcore.MeshCoreError as e:
        return {**_NOTHING, "problems": [f"{name}: {e}"]}
    if radio is None:
        return {**_NOTHING, "problems": [
            f"{name}: no meshcore.radio declared: its frequency and power would be the "
            f"firmware's defaults (10 dBm)"] if managed else []}
    problems = meshcore.check(name, block)
    # a node of this vocabulary boots receive-only: it transmits only when declared to
    if not radio.get("tx_enabled", False):
        return {"transmits": False, "emissions": [], "problems": problems}
    if radio.get("freq_mhz") is None or radio.get("bw_khz") is None:
        problems.append(f"{name}: transmits but declares no freq_mhz/bw_khz (or a preset)")
    return {"transmits": True, "problems": problems,
            "emissions": [{"radio": "lora", "key": "tx_dbm", "power_dbm": radio.get("tx_dbm"),
                           "freq_mhz": radio.get("freq_mhz"), "bw_khz": radio.get("bw_khz")}]}


def _write_meshtastic(name, lora, rules):
    out = []
    out += rfrules.region(name, REGION_CODES.get(lora.get("region"), lora.get("region")), rules)
    if "tx_power" in lora:
        out += rfrules.power(name, lora["tx_power"], rules, "tx_power", "lora")
    return out


def _write_meshcore(name, radio, rules):
    out = []
    if radio.get("tx_dbm") is not None:
        out += rfrules.power(name, radio["tx_dbm"], rules, "tx_dbm", "lora")
    if radio.get("freq_mhz") is not None and radio.get("bw_khz") is not None:
        out += rfrules.band(name, radio["freq_mhz"], radio["bw_khz"], rules, "lora")
    return out


WRITES = {"meshtastic": _write_meshtastic, "meshcore": _write_meshcore}


def check_write(vocabulary, name, block, rules=None, tx_now=None):
    """Violations in the radio block a module is about to write (a `meshtastic.config.lora`,
    or a `meshcore.radio` with its preset expanded). Only the keys in the block are judged: a
    key left out is left as the board has it. A block that switches the transmitter off writes
    nothing that keys up; one that does not say takes the board's own switch (`tx_now`; unknown
    counts as on)."""
    rules = {**DEFAULT_RULES, **(rules or {})}
    tx = block.get("tx_enabled")
    if tx is None:
        tx = True if tx_now is None else tx_now
    return WRITES[vocabulary](name, block, rules) if tx else []
