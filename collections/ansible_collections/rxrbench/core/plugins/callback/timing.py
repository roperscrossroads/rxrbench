"""How long each task and each module phase took, per board (docs/ARCHITECTURE.md#checking-depth)."""
import datetime
import json
import os
import time

from ansible import context
from ansible.plugins.callback import CallbackBase
from ansible_collections.rxrbench.core.plugins.plugin_utils import runrecord

DOCUMENTATION = r"""
name: timing
type: aggregate
short_description: Record how long each task and each module phase took, per board
description:
  - Enabled in the tool's C(ansible.cfg). Writes only for a play given C(bench_run) as an extra
    variable (the C(just) recipes of plan, apply, device tests and scenarios give one), to
    C(<runs>/<bench_run>/timing-<play>.json), where C(<play>) is the playbook's name, or C(plan)
    for C(apply.yml) in check mode.
  - Per board, each task that ran (not skipped) with its duration and status, and the phases a
    module returned in C(timing) (C(module_utils/timing.py)); C(just timing) reads them across
    runs.
"""

CALLBACK_VERSION = 2.0
CALLBACK_TYPE = "aggregate"
CALLBACK_NAME = "rxrbench.core.timing"
CALLBACK_NEEDS_ENABLED = True


class CallbackModule(CallbackBase):
    def __init__(self):
        super().__init__()
        self._play = None
        self._playbook = None
        self._started = {}
        self._tasks = {}
        self._wall = time.monotonic()

    def v2_playbook_on_start(self, playbook):
        self._playbook = os.path.splitext(os.path.basename(playbook._file_name))[0]  # pylint: disable=protected-access

    def v2_playbook_on_play_start(self, play):
        self._play = play

    def v2_runner_on_start(self, host, task):
        self._started[(host.get_name(), task._uuid)] = time.monotonic()  # pylint: disable=protected-access

    def _done(self, result, status):
        host, task = result._host.get_name(), result._task  # pylint: disable=protected-access
        start = self._started.pop((host, task._uuid), None)  # pylint: disable=protected-access
        if start is None:
            return
        entry = {"task": task.get_name(), "action": task.action, "status": status,
                 "seconds": round(time.monotonic() - start, 3)}
        phases = result._result.get("timing")  # pylint: disable=protected-access
        if isinstance(phases, list) and phases:
            entry["phases"] = json.loads(json.dumps(phases, default=str))
        self._tasks.setdefault(host, []).append(entry)

    def v2_runner_on_ok(self, result):
        self._done(result, "changed" if result._result.get("changed") else "ok")  # pylint: disable=protected-access

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._done(result, "failed")

    def v2_runner_on_unreachable(self, result):
        self._done(result, "unreachable")

    def v2_runner_on_skipped(self, result):
        host, task = result._host.get_name(), result._task  # pylint: disable=protected-access
        self._started.pop((host, task._uuid), None)  # pylint: disable=protected-access

    def _board(self, host, tasks):
        hv = runrecord.host_vars(self._play, host)
        return {"family": hv.get("bench_family"), "flasher": hv.get("bench_flasher"),
                "route": ((hv.get("bench_routes") or {}).get("flash") or {}).get("bearer"),
                "seconds": round(sum(t["seconds"] for t in tasks), 3), "tasks": tasks}

    def v2_playbook_on_stats(self, stats):
        if self._play is None:
            return
        where = runrecord.run_dir(self._play)
        if where is None:
            return
        run, out, first = where
        name = self._playbook or "play"
        if name == "apply" and context.CLIARGS.get("check"):
            name = "plan"
        record = {
            "v": 1,
            "run": run,
            "play": name,
            "layout": first.get("bench_layout"),
            "recorded": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seconds": round(time.monotonic() - self._wall, 3),
            "boards": {h: self._board(h, ts) for h, ts in sorted(self._tasks.items())},
        }
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, f"timing-{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
        self._display.display(f"timing: {path}")
