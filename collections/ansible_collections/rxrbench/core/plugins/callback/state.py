"""The state a run starts from, recorded from its plan (docs/ARCHITECTURE.md#runs-tests-and-campaigns)."""
import datetime
import json
import os

from ansible.plugins.callback import CallbackBase
from ansible_collections.rxrbench.core.plugins.plugin_utils import runrecord

DOCUMENTATION = r"""
name: state
type: aggregate
short_description: Record the state a run of tests starts from, from the plan before it
description:
  - Enabled for the plan that opens a run (C(just run)), with C(bench_run) given as an extra
    variable. Writes C(<runs>/<bench_run>/state.json) beside the run's test and scenario
    results.
  - Per board, the declared firmware, the image the plan read (any task result's C(image)),
    what the board reported (any task result's C(device); every flasher's plan returns one,
    and a plugin's update step that returns one is recorded the same way), the plan's change
    count and whether the board answered. A board that reported nothing is recorded as not
    reported, never guessed.
  - The declared image is the one C(firmware) names, or, when a layout pins C(bench_image)
    instead, that image's name as the board role staged it (C(board_image)), marked
    with C(image_from) set to C(bench_image). The depth the checks read at (C(checked)), the
    depth of the identity read (C(identity_checked); C(spot) when the board's last full read
    stood in for asking it) and what the survey found on the chip that the layout does not own
    (C(board_chip_findings), the plan's chip findings) are recorded too.
  - C(clean) is true when every board answered and the plan changed nothing; a run that is not
    clean is marked so, and a comparison of runs says it.
requirements:
  - C(bench_run) as an extra variable (C(-e bench_run=<time>)).
"""

CALLBACK_VERSION = 2.0
CALLBACK_TYPE = "aggregate"
CALLBACK_NAME = "rxrbench.core.state"
CALLBACK_NEEDS_ENABLED = True


def declared(firmware, staged):
    """The declared firmware, with the image named: `firmware`'s own, else the name of the image
    the board role staged (a layout that pins bench_image declares it there)."""
    out = dict(_plain(firmware or {}))
    if not out.get("image") and staged:
        out.update(image=os.path.basename(str(staged).rstrip("/")), image_from="bench_image")
    return out


def _plain(value):
    """Inventory values, out of Ansible's unsafe wrappers, as JSON can hold them."""
    return json.loads(json.dumps(value, default=str))


class CallbackModule(CallbackBase):
    def __init__(self):
        super().__init__()
        self._play = None
        self._boards = {}

    def _board(self, host):
        return self._boards.setdefault(host, {"changes": 0, "answered": True, "failed": False,
                                              "image": None, "device": None, "checked": None,
                                              "identity_checked": None, "staged": None,
                                              "foreign": None})

    def v2_playbook_on_play_start(self, play):
        self._play = play

    def _result(self, result, failed=False):
        board = self._board(result._host.get_name())
        res = result._result
        if failed:
            if res.get("unanswered"):
                board["answered"] = False
            else:
                board["failed"] = True
            return
        facts = res.get("ansible_facts") or {}
        if facts.get("board_image"):
            board["staged"] = str(facts["board_image"])
        if isinstance(facts.get("board_chip_findings"), list):
            board["foreign"] = _plain(facts["board_chip_findings"])
        if res.get("changed"):
            board["changes"] += 1
        if isinstance(res.get("image"), dict):
            board["image"] = _plain(res["image"])
        if isinstance(res.get("device"), dict):
            board["device"] = _plain(res["device"])
        if isinstance(res.get("checked"), dict):
            board["checked"] = _plain(res["checked"])
        if isinstance(res.get("identity_checked"), dict):
            board["identity_checked"] = _plain(res["identity_checked"])

    def v2_runner_on_ok(self, result):
        self._result(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._result(result, failed=True)

    def v2_runner_on_unreachable(self, result):
        self._board(result._host.get_name())["answered"] = False

    def v2_playbook_on_stats(self, stats):
        if self._play is None:
            return
        where = runrecord.run_dir(self._play)
        if where is None:
            self._display.warning("rxrbench.core.state: no bench_run given (-e bench_run=...): "
                                  "no state recorded")
            return
        run, out, first = where
        hosts = sorted(set(self._boards) | set(stats.processed))
        boards = {}
        for name in hosts:
            hv = runrecord.host_vars(self._play, name)
            b = self._board(name)
            boards[name] = {
                "layout": hv.get("bench_layout"),
                "family": hv.get("bench_family"),
                "declared": declared(hv.get("firmware"), b["staged"]),
                "image": b["image"],
                "reported": b["device"] if b["device"] is not None else "not reported",
                "checked": b["checked"],
                "identity_checked": b["identity_checked"],
                "foreign": b["foreign"] if b["foreign"] is not None else "not surveyed by this plan",
                "changes": b["changes"],
                "answered": b["answered"],
                "failed": b["failed"],
            }
        site = first.get("bench_site_dir")
        record = {
            "v": 1,
            "run": run,
            "recorded": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "layout": first.get("bench_layout"),
            "tool": runrecord.git(first.get("bench_repo") or os.getcwd()),
            "site": runrecord.git(site) if site else None,
            "boards": boards,
            "clean": all(b["answered"] and not b["failed"] and b["changes"] == 0
                         for b in boards.values()),
        }
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
        self._display.display(f"state of the bench: {os.path.join(out, 'state.json')}"
                              f" ({'clean' if record['clean'] else 'NOT clean: the bench differs from its declaration'})")
