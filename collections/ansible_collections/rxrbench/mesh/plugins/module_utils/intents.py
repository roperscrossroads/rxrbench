"""The roles a zephyrtastic node remembers (`meshtastic intents`), and the plan to forget them.

A node keeps its role intents in its config (Zephyr's settings store): the BLE peer it dials, the brain it reports
to, the heads it wants, the ear's receiving half, the courier's arm, the relay's direction,
a pinned preset list, a cluster document. None keys off a declared setting, so a board moved
from a rig layout to a plain one kept them. A plain profile declares
`zephyrtastic: {intents: none}`, and this brings the board to that: `meshtastic intents`
lists what is set, `meshtastic intents clear` forgets it all and reboots.

The listing, one intent per line, then a tally:

    peer     set   target 0x0a0b0c0d last 0x0a0b0c0d  (blepeer/central)
    head     none  (mtattach/brain)
    ...
    intents: 1 set
"""
import re

LINE = re.compile(r"^(?P<name>[a-z]+)\s+(?P<state>set|none)\b\s*(?P<detail>.*?)\s*(?:\((?P<keys>[^)]*)\))?\s*$")
TALLY = re.compile(r"^intents:\s+(\d+)\s+set\b")

INTENT_COMMAND = "meshtastic intents"
CLEAR_COMMAND = "meshtastic intents clear"


class IntentsError(Exception):
    pass


def parse(text):
    """{"set": {name: detail}, "none": [names], "count": n} from the listing, or IntentsError
    when the text is not one (an older firmware answers 'command not found')."""
    out = {"set": {}, "none": [], "count": None}
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("uart:~$"):
            continue
        m = TALLY.match(line)
        if m:
            out["count"] = int(m.group(1))
            continue
        m = LINE.match(line)
        if not m:
            continue
        if m.group("state") == "set":
            out["set"][m.group("name")] = m.group("detail")
        else:
            out["none"].append(m.group("name"))
    if out["count"] is None:
        raise IntentsError("not an intents listing (firmware without `meshtastic intents`?): "
                           + str(text).strip()[-120:])
    if out["count"] != len(out["set"]):
        raise IntentsError(f"the listing tallies {out['count']} set but names {len(out['set'])}")
    return out


def plan(declared, listing):
    """(commands, before, after) for a declared `intents:` value against the listing. Only
    `none` is a declaration today: everything else a layout wants is declared by the module
    that sets it (brain, listener, ...), and this one must not be asked to forget it."""
    if declared != "none":
        raise IntentsError(f"intents: {declared!r} is not a declaration this tool knows; `none` "
                           "is the only one (a role is declared by its own setting)")
    before = dict(listing["set"])
    if not before:
        return [], before, {}
    return [CLEAR_COMMAND], before, {}


def conflicts(zephyrtastic):
    """The keys of a board's `zephyrtastic:` block that declare a role, when it also declares
    `intents: none`: the two cannot both be meant (clear, then set, every apply)."""
    if (zephyrtastic or {}).get("intents") != "none":
        return []
    return sorted(k for k in ("brain", "head", "listener", "cluster", "ear", "relay", "courier")
                  if k in (zephyrtastic or {}))
