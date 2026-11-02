"""How long the phases of a module's work take (docs/ARCHITECTURE.md#checking-depth).

A module times its phases and returns them as `timing`; the `rxrbench.core.timing` callback
puts them beside the task's own time in `runs/<run>/timing.json`, and `just timing` reads them
across runs. Any module may use it, a plugin's included:

    phases = timing.Phases()
    with phases.phase("read"):
        ...
    module.exit_json(..., timing=phases.out)

A phase that raises is recorded too, with `failed: true`, so a slow refusal is seen as such.
"""
import contextlib
import time


class Phases:
    def __init__(self):
        self.out = []

    @contextlib.contextmanager
    def phase(self, name):
        start = time.monotonic()
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            entry = {"phase": name, "seconds": round(time.monotonic() - start, 3)}
            if failed:
                entry["failed"] = True
            self.out.append(entry)
