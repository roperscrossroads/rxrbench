"""What a brain stores for its heads, and what a listener stores to sweep.

Both are config the firmware keeps across a reboot (in Zephyr's settings store), and both are read back from the
state line: a brain's `brain.heads[].w` (the preset it wants of each head, and re-sends
whenever that head reports another) and a listener's `scan.presets`. `plan_heads()` and
`plan_scan()` are pure: declared and actual state in, shell commands and a diff out.

A brain names a head by its attachment number, which the brain hands out as heads arrive; the
wish is stored by the head's node id. A layout names a head by its board, so the caller
resolves board -> node id (from the head's own state line) before planning.
"""
from . import enums, state

PRESET_NUMBER = {v: k for k, v in enums.MODEM_PRESET.items()}


class AttachError(ValueError):
    pass


def node_key(node_id):
    """A node id as one comparable form, whatever its spelling (`0x0a1a2a3a`, `0A1A2A3A`)."""
    try:
        return int(str(node_id), 16)
    except ValueError as e:
        raise AttachError(f"{node_id!r} is not a node id") from e


def check_heads(declared):
    for board, preset in declared.items():
        if preset not in PRESET_NUMBER:
            raise AttachError(f"head {board}: {preset!r} is not a preset "
                              f"(one of {', '.join(sorted(PRESET_NUMBER))})")


def plan_heads(declared, ids, brain):
    """Commands that make a brain want, of each declared head, the declared preset.

    declared: {head board: PRESET}; ids: {head board: node id}; brain: the `brain` facts.
    A head the brain does not hold in its table cannot be named to it, and is an error. An
    attached head the layout does not declare is left as it is.
    """
    check_heads(declared)
    table = {node_key(h["node_id"]): h for h in brain.get("heads", [])}
    missing = [b for b in declared if node_key(ids[b]) not in table]
    if missing:
        raise AttachError("not attached to this brain: " + ", ".join(
            f"{b} ({ids[b]})" for b in sorted(missing)))
    cmds, delta = [], {}
    for board in sorted(declared):
        head = table[node_key(ids[board])]
        want = declared[board]
        if head.get("wanted_preset") != want:
            delta[board] = (head.get("wanted_preset"), want)
            cmds.append(f"meshtastic attach preset {head['attachment']} {PRESET_NUMBER[want]}")
    return cmds, delta


def heads_left(declared, ids, brain, retuned=True):
    """{board: why} for every declared head not yet as declared. With `retuned`, a head whose
    link is up must also be on the preset (the brain asks it; it answers when it has moved)."""
    table = {node_key(h["node_id"]): h for h in brain.get("heads", [])}
    left = {}
    for board, want in declared.items():
        head = table.get(node_key(ids[board]))
        if head is None:
            left[board] = "not attached"
        elif head.get("wanted_preset") != want:
            left[board] = f"wanted {head.get('wanted_preset')}, declared {want}"
        elif retuned and head.get("link_up") and head.get("preset") != want:
            left[board] = f"wanted {want} but still on {head.get('preset')}"
    return left


def check_scan(declared):
    if not declared:
        raise AttachError("a listener's preset list is empty; declare at least one preset")
    bad = [p for p in declared if p not in state.PRESET_DISPLAY]
    if bad:
        raise AttachError(f"cannot be swept: {', '.join(map(repr, bad))} "
                          f"(one of {', '.join(sorted(state.PRESET_DISPLAY))})")
    if len(set(declared)) != len(declared):
        raise AttachError("a preset is listed twice")


def plan_scan(declared, scanner):
    """The command that makes a listener sweep, and store, exactly the declared list (order
    included: it is the order of the sweep)."""
    check_scan(declared)
    actual = list(scanner.get("presets") or [])
    if actual == list(declared):
        return [], None
    return ["meshtastic scan presets " + " ".join(state.PRESET_DISPLAY[p] for p in declared)], \
        (actual, list(declared))

