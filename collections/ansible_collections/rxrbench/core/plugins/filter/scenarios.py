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


class FilterModule:
    def filters(self):
        return {"scenarios_with_counters": with_counters}
