"""What a hub stores for its workers, and what a listener stores to sweep.

Both are config the firmware keeps across a reboot (in Zephyr's settings store), and both are read back from the
state line: a hub's `hub.workers[].w` (the preset it wants of each worker, and re-sends
whenever that worker reports another) and a listener's `scan.presets`. `plan_workers()` and
`plan_scan()` are pure: declared and actual state in, shell commands and a diff out.

A hub names a worker by its number, which the hub hands out as workers arrive; the wish is
stored by the worker's node id. A layout names a worker by its board, so the caller resolves
board -> node id (from the worker's own state line) before planning.

Renamed (brain -> hub, head -> worker). For one release both spellings are read:
a layout's `zephyrtastic: {brain: {heads: ...}}` and `{head: {brain: X}}` (`declared_workers()`,
`declared_hub()`), and the firmware's shell verbs, new first (`hub preset`), then the old one
(`attach preset`) on a board that does not know the new one (`OLD_VERBS`).
"""
import re

from . import enums, state

PRESET_NUMBER = {v: k for k, v in enums.MODEM_PRESET.items()}

#: The shell verbs that were renamed: the new spelling -> the one firmware from before the
#: rename answers to (0.4.79 and older).
OLD_VERBS = {"meshtastic hub preset": "meshtastic attach preset"}

#: What a Zephyr shell answers to a verb it does not have.
UNKNOWN_COMMAND = re.compile(r"command not found|Unknown parameter|wrong parameter count|"
                             r"Subcommand not found", re.I)


class HubError(ValueError):
    pass


def declared_workers(zephyrtastic):
    """{worker board: preset} a board's `zephyrtastic:` block declares it a hub of (`hub:
    {workers: ...}`, or the old `brain: {heads: ...}`), or None when it declares none."""
    zep = zephyrtastic or {}
    for key, sub in (("hub", "workers"), ("brain", "heads")):
        if isinstance(zep.get(key), dict) and zep[key].get(sub) is not None:
            return dict(zep[key][sub])
    return None


def declared_hub(zephyrtastic):
    """The hub board a board's block declares it a worker of (`worker: {hub: X}`, or the old
    `head: {brain: X}`), or None."""
    zep = zephyrtastic or {}
    for key, sub in (("worker", "hub"), ("head", "brain")):
        if isinstance(zep.get(key), dict) and zep[key].get(sub) is not None:
            return zep[key][sub]
    return None


def is_worker(zephyrtastic):
    """Does the block declare a (keyless) worker, in either spelling?"""
    zep = zephyrtastic or {}
    return bool(zep.get("worker") or zep.get("head"))


def run(board, cmd, deadline):
    """Run a shell command in its new spelling; on a board whose firmware answers that it
    does not know it, run the old spelling instead. Returns the answer."""
    out = board.exec(cmd, deadline=deadline)
    for new, old in OLD_VERBS.items():
        if cmd.startswith(new + " ") and UNKNOWN_COMMAND.search(str(out or "")):
            return board.exec(old + cmd[len(new):], deadline=deadline)
    return out


def node_key(node_id):
    """A node id as one comparable form, whatever its spelling (`0x0a1a2a3a`, `0A1A2A3A`)."""
    try:
        return int(str(node_id), 16)
    except ValueError as e:
        raise HubError(f"{node_id!r} is not a node id") from e


def check_workers(declared):
    for board, preset in declared.items():
        if preset not in PRESET_NUMBER:
            raise HubError(f"worker {board}: {preset!r} is not a preset "
                           f"(one of {', '.join(sorted(PRESET_NUMBER))})")


def plan_workers(declared, ids, hub):
    """Commands that make a hub want, of each declared worker, the declared preset.

    declared: {worker board: PRESET}; ids: {worker board: node id}; hub: the `hub` facts.
    A worker the hub does not hold in its table cannot be named to it, and is an error. A
    linked worker the layout does not declare is left as it is.
    """
    check_workers(declared)
    table = {node_key(w["node_id"]): w for w in hub.get("workers", [])}
    missing = [b for b in declared if node_key(ids[b]) not in table]
    if missing:
        raise HubError("not linked to this hub: " + ", ".join(
            f"{b} ({ids[b]})" for b in sorted(missing)))
    cmds, delta = [], {}
    for board in sorted(declared):
        worker = table[node_key(ids[board])]
        want = declared[board]
        if worker.get("wanted_preset") != want:
            delta[board] = (worker.get("wanted_preset"), want)
            cmds.append(f"meshtastic hub preset {worker['number']} {PRESET_NUMBER[want]}")
    return cmds, delta


def workers_left(declared, ids, hub, retuned=True):
    """{board: why} for every declared worker not yet as declared. With `retuned`, a worker
    whose link is up must also be on the preset (the hub asks it; it answers when it has
    moved)."""
    table = {node_key(w["node_id"]): w for w in hub.get("workers", [])}
    left = {}
    for board, want in declared.items():
        worker = table.get(node_key(ids[board]))
        if worker is None:
            left[board] = "not linked"
        elif worker.get("wanted_preset") != want:
            left[board] = f"wanted {worker.get('wanted_preset')}, declared {want}"
        elif retuned and worker.get("link_up") and worker.get("preset") != want:
            left[board] = f"wanted {want} but still on {worker.get('preset')}"
    return left


def check_scan(declared):
    if not declared:
        raise HubError("a listener's preset list is empty; declare at least one preset")
    bad = [p for p in declared if p not in state.PRESET_DISPLAY]
    if bad:
        raise HubError(f"cannot be swept: {', '.join(map(repr, bad))} "
                       f"(one of {', '.join(sorted(state.PRESET_DISPLAY))})")
    if len(set(declared)) != len(declared):
        raise HubError("a preset is listed twice")


def plan_scan(declared, scanner):
    """The command that makes a listener sweep, and store, exactly the declared list (order
    included: it is the order of the sweep)."""
    check_scan(declared)
    actual = list(scanner.get("presets") or [])
    if actual == list(declared):
        return [], None
    return ["meshtastic scan presets " + " ".join(state.PRESET_DISPLAY[p] for p in declared)], \
        (actual, list(declared))
