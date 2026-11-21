"""Counters read around a scenario.

A scenario that asks for it (`counters: true`, or every scenario under the layout's
`bench_scenario_counters`, unless one says `counters: false`) is given a reading of each of its
boards' counters before its first step and another after its last; the second is judged against
the first by `counter_delta` (plugins/filter/counters.py). Which boards have counters, the
command that reads them and how they are judged come from each board's family
(`bench_health_counters`, published from `Family.health_counters`): core knows no firmware.

How much a judged counter may grow is the family's default, then the board's layout vars
(`bench_counters_allow`, `bench_counters_ignore`), then the scenario's own (`counters: {allow,
ignore}`), each merged over the one before. The readings after are `always` steps: they run
even when a step before them failed, so a failed scenario still shows what its boards counted.
Runs on the controller; reads the boards' variables, never a device.
"""

#: The name the reading before is kept under, for the reading after to compare with.
KEPT = "counters"


def wanted(scenario, default=False):
    """Whether the scenario asks for the readings: its `counters`, else the layout's default."""
    c = scenario.get("counters", None)
    if c is None:
        return bool(default)
    return c is not False


def merged_spec(spec, board_allow=None, board_ignore=None, scenario_opts=None):
    """The family's spec with the board's and the scenario's allowances merged over it."""
    out = dict(spec)
    allow = dict(spec.get("allow") or {})
    ignore = list(spec.get("ignore") or [])
    sc = scenario_opts if isinstance(scenario_opts, dict) else {}
    for more_allow, more_ignore in ((board_allow, board_ignore), (sc.get("allow"), sc.get("ignore"))):
        allow.update(more_allow or {})
        ignore += [k for k in (more_ignore or []) if k not in ignore]
    out["allow"], out["ignore"] = allow, ignore
    return out


def with_counters(scenario, boards, default=False):
    """The scenario with its readings around it, or as it was when it asks for none.

    `boards` maps a board's name to its vars ({bench_health_counters, bench_counters_allow,
    bench_counters_ignore}); a board missing from it, or whose family has no counters, is not
    read."""
    if not wanted(scenario, default):
        return scenario
    before, after = [], []
    seen = []
    for step in scenario.get("steps") or []:
        b = step.get("board")
        if b in seen:
            continue
        seen.append(b)
        hv = boards.get(b) or {}
        spec = hv.get("bench_health_counters")
        if not spec:
            continue
        delta = merged_spec({k: v for k, v in spec.items() if k != "cmd"},
                            hv.get("bench_counters_allow"), hv.get("bench_counters_ignore"),
                            scenario.get("counters"))
        before.append({"name": f"{b} counters before", "board": b, "cmd": spec["cmd"], "keep": KEPT})
        after.append({"name": f"{b} counters after", "board": b, "cmd": spec["cmd"],
                      "delta": {**delta, "against": KEPT}, "always": True})
    if not before:
        return scenario
    return {**scenario, "steps": before + list(scenario["steps"]) + after}
