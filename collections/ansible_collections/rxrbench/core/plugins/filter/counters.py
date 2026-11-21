"""Counters read twice, judged by how much they moved: a scenario step's `delta`.

A device that keeps counters (frames sent, retries, frames dropped, a radio reset) is judged by
the change across a test, not by the totals: a counter that has counted since boot says nothing
about the minutes a test ran. Two outputs of the same command, a spec that says where the
counters are in them, which of them are judged, and how much each may move:

    extract   a regex whose first group is a JSON object in the output (default: the whole
              output is the JSON)
    path      dotted keys to the object of counters inside it (default: the top)
    judge     a key of that object holding the counters whose growth fails the step (a device
              that sorts its counters, as operations, retries, warnings and errors, names its
              errors here); default: the object's own numbers are judged
    record    keys of that object holding counters whose changes are kept and reported, never
              judged (the operations, retries and warnings beside the errors)
    ignore    judged keys that are not judged after all (a gauge, a count of work)
    allow     {key: n}: how much a judged key may grow; `*` is every key not named (default 0)
    restart   a key of the object that only grows while the device runs (uptime): when it went
              down, the counters started again between the readings and the deltas mean
              nothing, which is a failure in itself (the device reset during the test)
    optional  true: a first reading without the counters (firmware from before them) is not
              judged; the result says so in `note`

Returns {"deltas": {key: change} of the judged counters, "recorded": {group: {key: change}},
"why": [reasons the change is not allowed]}, and "note" when it judged nothing. A key in the
second reading and not the first counts from 0; a value that is not a number is skipped. A
reading that cannot be read (no match, not JSON, no such path, none at all) is a reason too,
not an error: the step that asked fails, the play goes on.
"""
import json
import re

from ansible.errors import AnsibleFilterError


def _numbers(obj):
    return {k: v for k, v in (obj or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _object(text, spec, which):
    """The object of counters in one reading (path applied), or AnsibleFilterError."""
    text = text or ""
    rx = spec.get("extract")
    if rx:
        m = re.search(rx, text, re.S)
        if not m:
            raise AnsibleFilterError(f"counter_delta: the {which} reading has nothing that matches /{rx}/")
        text = m.group(1)
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise AnsibleFilterError(f"counter_delta: the {which} reading is not JSON: {e}") from e
    for key in [k for k in str(spec.get("path") or "").split(".") if k]:
        if not isinstance(obj, dict) or key not in obj:
            raise AnsibleFilterError(f"counter_delta: the {which} reading has no {spec['path']!r}")
        obj = obj[key]
    if not isinstance(obj, dict):
        raise AnsibleFilterError(f"counter_delta: the {which} reading's counters are not an object")
    judge = spec.get("judge")
    if judge and not isinstance(obj.get(judge), dict):
        raise AnsibleFilterError(f"counter_delta: the {which} reading has no group {judge!r}")
    return obj


def _change(after, before):
    return {k: after[k] - before.get(k, 0) for k in after}


def counter_delta(after, before, spec=None):
    """The counters' change from `before` to `after` (two outputs), judged by `spec`."""
    spec = dict(spec or {})
    out = {"deltas": {}, "recorded": {}, "why": []}
    if before is None:
        return {**out, "why": ["no first reading to compare with (no step kept it)"]}
    try:
        b = _object(before, spec, "first")
    except AnsibleFilterError as e:
        if spec.get("optional"):
            return {**out, "note": f"not judged: {e}"}
        return {**out, "why": [str(e)]}
    try:
        a = _object(after, spec, "second")
    except AnsibleFilterError as e:
        return {**out, "why": [str(e)]}

    judge = spec.get("judge")
    restart = spec.get("restart")
    ja, jb = (_numbers(a[judge]), _numbers(b[judge])) if judge else (_numbers(a), _numbers(b))
    out["deltas"] = _change(ja, jb)
    for group in spec.get("record") or ():
        if isinstance(a.get(group), dict):
            out["recorded"][group] = _change(_numbers(a[group]), _numbers(b.get(group) or {}))

    top_a, top_b = _numbers(a), _numbers(b)
    if restart and restart in top_a and restart in top_b and top_a[restart] < top_b[restart]:
        out["why"].append(f"{restart} went from {top_b[restart]} to {top_a[restart]}: the device "
                          "restarted between the readings, its counters with it")
        return out
    ignore = set(spec.get("ignore") or ())
    allow = dict(spec.get("allow") or {})
    default = allow.pop("*", 0)
    for k in sorted(out["deltas"]):
        if k in ignore or k == restart:
            continue
        limit = allow.get(k, default)
        if out["deltas"][k] > limit:
            out["why"].append(f"{k} grew by {out['deltas'][k]} ({jb.get(k, 0)} -> {ja[k]})"
                              + (f", {limit} allowed" if limit else ""))
    return out


class FilterModule:
    def filters(self):
        return {"counter_delta": counter_delta}
