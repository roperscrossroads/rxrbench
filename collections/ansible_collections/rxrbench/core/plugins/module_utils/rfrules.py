"""The RF rules: a site says its region, and nothing transmits outside it.

A site states where it is, once (`<site>/rf_rules.yml`, or a file a layout names as
`rf_ceiling`):

    region: US

The region gives each kind of radio the band a transmitter must stay inside and the most it
may put out. Those numbers are not here: a plugin brings them for the radios its devices
have (`register_radio`). Until a region is set, a layout whose boards transmit is refused: a radio stays
off until the tool knows where it is. A site may ask for less than its region allows
(`max_tx_power`, a narrower `band_mhz`), never more, and a layout's own `rf_rules` may only
tighten what the site says (`tighten`): the file that declares the transmitters cannot also
raise their limit.

This module is what code on a bench host shares with the layout check: the regions, the
ceiling and the measurements. What a board declares is its family's to say (rflayout.py asks
each family's plugin).
"""
import os

#: What a region allows each kind of radio: {radio: {region: {band_mhz: [low, high],
#: max_tx_power: dBm}}}. A plugin's module_utils registers the radios its devices have when it
#: is imported, on the controller and on a bench host alike. They keep a transmitter in the
#: right band at a permitted power; they are not the whole of any country's rules (duty cycle,
#: channel width and antenna gain are the firmware's and the site's to respect).
RADIOS = {}


def register_radio(radio, regions):
    """A plugin's limits for one kind of radio (`lora`, `ble`, `wifi`), by region."""
    RADIOS[radio] = {r: dict(v) for r, v in regions.items()}


def limits(region, radio=None):
    """What `region` allows `radio`, or, with no radio named, the most it allows any radio
    registered ({} when it allows none a band and a power)."""
    if radio is not None:
        return (RADIOS.get(radio) or {}).get(region) or {}
    found = [t[region] for t in RADIOS.values() if region in t]
    if not found:
        return {}
    out = {"max_tx_power": max(f["max_tx_power"] for f in found)}
    if len(found) == 1:
        out["band_mhz"] = list(found[0]["band_mhz"])
    return out


def regions():
    """The regions any registered radio has limits for."""
    return sorted({r for t in RADIOS.values() for r in t})


CORE_FIELDS = {
    "region": {"kind": "fixed", "default": None},
    "max_tx_power": {"kind": "lower", "default": None},
    "band_mhz": {"kind": "narrow", "default": None},
}
DEFAULT_RULES = {k: v["default"] for k, v in CORE_FIELDS.items()}


def defaults():
    return dict(DEFAULT_RULES)


def with_region(rules):
    """`rules` with what its region gives where it states nothing itself. Raises ValueError
    for a region not known here, or a rule that asks for more than the region allows."""
    region = rules.get("region")
    if region is None or not RADIOS:
        return rules
    if region not in regions():
        raise ValueError(f"unknown region {region!r}; the regions are {', '.join(regions())}")
    limit, out = limits(region), dict(rules)
    if out.get("max_tx_power") is None:
        out["max_tx_power"] = limit["max_tx_power"]
    elif out["max_tx_power"] > limit["max_tx_power"]:
        raise ValueError(f"max_tx_power {out['max_tx_power']} dBm is above what region {region} "
                         f"allows ({limit['max_tx_power']} dBm)")
    band = limit.get("band_mhz")
    if band and out.get("band_mhz") is None:
        out["band_mhz"] = list(band)
    elif band and (out["band_mhz"][0] < band[0] or out["band_mhz"][1] > band[1]):
        raise ValueError(f"band_mhz {out['band_mhz'][0]}-{out['band_mhz'][1]} reaches outside "
                         f"region {region}'s {band[0]}-{band[1]} MHz")
    return out


def ceiling(path=None):
    """The rules nothing may loosen: the file at `path` (each field optional), with what its
    region gives for the power and the band it does not state. No file, or no region in it:
    no region, and nothing may transmit."""
    rules = defaults()
    if path and os.path.exists(path):
        import yaml  # pylint: disable=import-outside-toplevel
        with open(path) as fh:
            doc = yaml.safe_load(fh) or {}
        unknown = sorted(set(doc) - set(rules))
        if unknown:
            raise ValueError(f"{path}: unknown RF rule(s) {', '.join(unknown)}; the fields are "
                             f"{', '.join(rules)}")
        rules.update({k: v for k, v in doc.items() if v is not None})
        try:
            rules = with_region(rules)
        except ValueError as e:
            raise ValueError(f"{path}: {e}") from e
    return rules


def tighten(ceil, layout_rules):
    """(rules, problems): the ceiling with a layout's `rf_rules` applied where they tighten it.
    A rule that loosens the ceiling is a problem (and the ceiling's value stands)."""
    rules, problems = dict(ceil), []
    own = layout_rules or {}
    known = CORE_FIELDS
    unknown = sorted(set(own) - set(known))
    if unknown:
        problems.append(f"rf_rules: unknown rule(s) {', '.join(unknown)}; the fields are {', '.join(known)}")
    for name in own:           # in the order the layout states them
        if name not in known:
            continue
        field, want, top = known[name], own[name], ceil[name]
        if field["kind"] == "fixed":
            if top is not None and want != top:
                problems.append(f"rf_rules.{name} {want} is not the site's ({top}): a layout "
                                f"does not move a bench to another {name}")
            else:
                rules[name] = want
        elif top is None:
            rules[name] = want          # the site states no limit of this kind: the layout's is one
        elif field["kind"] == "lower":
            if want > top:
                problems.append(f"rf_rules.{name} {want} dBm is above the ceiling of {top} dBm: "
                                f"a layout may only lower it")
            else:
                rules[name] = want
        else:
            lo, hi = want
            if lo < top[0] or hi > top[1]:
                problems.append(f"rf_rules.{name} {lo}-{hi} reaches outside the ceiling's "
                                f"{top[0]}-{top[1]} MHz: a layout may only narrow it")
            else:
                rules[name] = [lo, hi]
    try:
        rules = with_region(rules)      # a layout that set the region gets what it gives
    except ValueError as e:
        problems.append(f"rf_rules: {e}")
    return rules, problems


def power(name, dbm, rules, key, radio=None):
    """Problems with a declared transmit power (`key` is what the layout calls it) of one
    kind of radio. No power declared, or 0, is the firmware's own choice, its region's maximum:
    that is within the rules only while they ask for no less than the region allows."""
    allowed = limits(rules.get("region"), radio).get("max_tx_power")
    caps = [c for c in (rules.get("max_tx_power"), allowed) if c is not None]
    if not caps:
        return []
    cap = min(caps)
    if dbm is not None and (not isinstance(dbm, int) or isinstance(dbm, bool)):
        return [f"{name}: {key} must be an integer, got {dbm!r}"]
    if not dbm:
        if cap >= (allowed if allowed is not None else float("inf")):
            return []
        return [f"{name}: transmits but declares no {key}" if dbm is None else
                f"{name}: {key} 0 means the region maximum; the ceiling is {cap} dBm"]
    if dbm > cap:
        return [f"{name}: {key} {dbm} dBm is above the ceiling of {cap} dBm"]
    return []


def band(name, freq_mhz, bw_khz, rules, radio=None):
    """Problems with a declared channel of one kind of radio: all of it must sit inside the
    band (the site's, where it states one, else the region's for that radio)."""
    limit = rules.get("band_mhz") or limits(rules.get("region"), radio).get("band_mhz")
    if limit is None:
        return []
    lo, hi = float(freq_mhz) - float(bw_khz) / 2000.0, float(freq_mhz) + float(bw_khz) / 2000.0
    if lo < limit[0] or hi > limit[1]:
        return [f"{name}: its channel {lo:.4f}-{hi:.4f} MHz is outside the band "
                f"{limit[0]}-{limit[1]} MHz"]
    return []


def region(name, declared, rules):
    """Problems with the region a board's radio is declared to be set to (a region code of
    a registered radio's, or the firmware's own name for one this tool does not know)."""
    if declared is not None and rules.get("region") is not None and declared != rules["region"]:
        return [f"{name}: its radio is declared for region {declared}; this site is in "
                f"{rules['region']}"]
    return []


def check_emissions(name, emissions, rules):
    """Problems with what a board is declared to emit: [{radio, key, power_dbm, region?,
    freq_mhz?, bw_khz?}]. Nothing transmits before the site has said where it is; then the region, the
    power and (where a frequency and a bandwidth are stated) the band are judged."""
    if emissions and not rules.get("region"):
        return [f"{name}: transmits, and no region is set: say `region: US` (or "
                f"{', '.join(r for r in regions() if r != 'US')}) in the site's rf_rules.yml"]
    out = []
    for e in emissions:
        out += region(name, e.get("region"), rules)
        out += power(name, e.get("power_dbm"), rules, e.get("key") or "power", e.get("radio"))
        if e.get("freq_mhz") is not None and e.get("bw_khz") is not None:
            out += band(name, e["freq_mhz"], e["bw_khz"], rules, e.get("radio"))
    return out
