"""Scenario helpers for playbooks/scenario.yml (the logic is module_utils/readings.py)."""
from ansible_collections.rxrbench.core.plugins.module_utils import readings

#: The board vars the readings need, and nothing else (hostvars are read lazily).
_KEYS = ("bench_health_counters", "bench_counters_allow", "bench_counters_ignore")


def with_counters(scenarios, hostvars, default=False):
    """Each scenario with its counter readings around it, when it asks for them."""
    names = {s.get("board") for sc in scenarios for s in (sc.get("steps") or [])}
    boards = {}
    for n in names:
        if n in hostvars:
            hv = hostvars[n]
            boards[n] = {k: hv.get(k) for k in _KEYS}
    return [readings.with_counters(sc, boards, default) for sc in scenarios]


#: what a step hands its command(s) on to device_run with
_COMMAND_KEYS = ("expect", "refuse", "rc", "timeout", "await", "limits")


def step_commands(step, step_name):
    """The device_run commands of one scenario step: its `cmd`, or each of its `cmds` (a list
    of {cmd, expect, refuse, rc, timeout, await, limits}) named <step>-NN."""
    if step.get("cmds") is not None:
        cmds = step["cmds"]
        if not isinstance(cmds, list) or not cmds:
            raise ValueError(f"{step_name}: cmds is a list of commands")
        out = []
        for i, c in enumerate(cmds):
            c = {"cmd": c} if isinstance(c, str) else dict(c)
            out.append({"name": "%s-%02d" % (step_name, i + 1), "cmd": c.get("cmd"),
                        **{k: c[k] for k in _COMMAND_KEYS if c.get(k) is not None}})
        return out
    return [{"name": step_name, "cmd": step.get("cmd"),
             **{k: step[k] for k in _COMMAND_KEYS if step.get(k) is not None}}]


def step_merged(results, step_name):
    """One step's result from what its commands said: passed when every one did, its output each
    command's after a `> <cmd>` line, its reasons each prefixed with the command's number, its
    values every command's (a later one's win)."""
    if len(results) == 1 and results[0].get("name") == step_name:
        return results[0]
    why, values, out = [], {}, []
    for i, r in enumerate(results):
        out.append("> " + r.get("cmd", ""))
        if r.get("output"):
            out.append(r["output"])
        why += [f"{i + 1}: {w}" for w in r.get("why") or []]
        values.update(r.get("values") or {})
    merged = {"name": step_name, "cmd": "\n".join(r.get("cmd", "") for r in results),
              "output": "\n".join(out), "test": any(r.get("test") for r in results),
              "passed": bool(results) and all(r.get("passed") for r in results), "why": why,
              "commands": len(results)}
    if values:
        merged["values"] = values
    return merged


class FilterModule:
    def filters(self):
        return {"scenarios_with_counters": with_counters, "step_commands": step_commands,
                "step_merged": step_merged}
