"""Run named commands on a device and judge their output: what diagnostics and tests share.

A command is `{name, cmd}` plus, for a test, what its output must contain (`expect`, a
regular expression), must not contain (`refuse`), and for a device with exit codes the code
it must return (`rc`, default 0). A command with none of those is a diagnostic: its output
is collected and it cannot fail, except by not running at all.

A command that starts something whose result the device prints later (a test, a transfer, a
tool run) names that result: `await: {pattern, within}`, a regular expression a later console
line must match within that many seconds (default 30). The command is sent once; its output is
then what the console said after it, up to that line. `limits: {<group>: {min, max, eq}}` judge
the numbers the expression captures by name (await's, else expect's): `rc (?P<rc>-?\\d+)`
with `{rc: {eq: 0}}`. What was captured is returned as `values`.

`run_all()` takes the transport as a function `execute(cmd, timeout) -> (rc or None, output)`,
so the same judging serves a board's shell (no exit codes) and a Linux device over ssh; a
command with `await` also needs `awaiter`, a reader of the board's console log
(module_utils/consolelog.py), which only a board has.
"""
import re

KEYS = {"name", "cmd", "expect", "refuse", "rc", "timeout", "await", "limits"}
LIMITS = {"min", "max", "eq"}
AWAIT_WITHIN = 30.0


class RunnerError(ValueError):
    pass


def check(commands):
    seen = set()
    for c in commands:
        extra = set(c) - KEYS
        if extra or not c.get("name") or not c.get("cmd"):
            raise RunnerError(f"a command needs name and cmd, and may have "
                              f"{', '.join(sorted(KEYS - {'name', 'cmd'}))}: {c!r}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", c["name"]):
            raise RunnerError(f"{c['name']!r}: a name is letters, digits, '.', '_' and '-' "
                              f"(it becomes a file name)")
        if c["name"] in seen:
            raise RunnerError(f"{c['name']!r} is named twice")
        seen.add(c["name"])
        for k in ("expect", "refuse"):
            if c.get(k) is not None:
                try:
                    re.compile(c[k])
                except re.error as e:
                    raise RunnerError(f"{c['name']}: {k} is not a regular expression: {e}") from e
        aw = c.get("await")
        if aw is not None:
            if not isinstance(aw, dict) or not aw.get("pattern") or set(aw) - {"pattern", "within"}:
                raise RunnerError(f"{c['name']}: await is {{pattern, within}}: {aw!r}")
            try:
                re.compile(aw["pattern"])
            except re.error as e:
                raise RunnerError(f"{c['name']}: await's pattern is not a regular expression: {e}") from e
            try:
                if float(aw.get("within", AWAIT_WITHIN)) <= 0:
                    raise ValueError
            except (TypeError, ValueError) as e:
                raise RunnerError(f"{c['name']}: await's within is a number of seconds above 0") from e
        limits = c.get("limits")
        if limits is not None:
            source = (aw or {}).get("pattern") or c.get("expect")
            if not isinstance(limits, dict) or not limits or source is None:
                raise RunnerError(f"{c['name']}: limits is {{<group>: {{min, max, eq}}}}, on the "
                                  "groups await's pattern (or expect) captures by name")
            groups = re.compile(source).groupindex
            for g, lim in limits.items():
                if g not in groups:
                    raise RunnerError(f"{c['name']}: limits names {g!r}, which /{source}/ does not "
                                      f"capture (it captures: {', '.join(groups) or 'nothing by name'})")
                if not isinstance(lim, dict) or not lim or set(lim) - LIMITS:
                    raise RunnerError(f"{c['name']}: limits.{g} is {{min, max, eq}}: {lim!r}")
                for k, v in lim.items():
                    # a templated layout may hand a number on as its text
                    if isinstance(v, bool) or not isinstance(number(str(v)), (int, float)):
                        raise RunnerError(f"{c['name']}: limits.{g}.{k} is a number: {v!r}")


def number(text):
    """A captured value as a number when it is one, else as it was."""
    try:
        return int(text, 0)
    except (TypeError, ValueError):
        try:
            return float(text)
        except (TypeError, ValueError):
            return text


def judge_limits(limits, values):
    why = []
    for g, lim in (limits or {}).items():
        lim = {k: number(str(x)) for k, x in lim.items()}
        v = values.get(g)
        if not isinstance(v, (int, float)):
            why.append(f"{g} is {v!r}, not a number")
            continue
        if "eq" in lim and v != lim["eq"]:
            why.append(f"{g} {v}, wanted {lim['eq']}")
        if "min" in lim and v < lim["min"]:
            why.append(f"{g} {v}, under {lim['min']}")
        if "max" in lim and v > lim["max"]:
            why.append(f"{g} {v}, over {lim['max']}")
    return why


def captured(match):
    return {k: number(v) for k, v in match.groupdict().items() if v is not None} if match else {}


def judge(command, rc, output):
    """Why a command's result fails its expectations: a list of reasons, empty when it passes."""
    why = []
    if rc is not None and is_test(command):
        want = command.get("rc", 0)
        if rc != want:
            why.append(f"exit code {rc}, wanted {want}")
    if command.get("expect") is not None and not re.search(command["expect"], output, re.M):
        why.append(f"output does not match /{command['expect']}/")
    if command.get("refuse") is not None and re.search(command["refuse"], output, re.M):
        why.append(f"output matches /{command['refuse']}/")
    return why


def is_test(command):
    return any(command.get(k) is not None for k in ("expect", "refuse", "rc", "await", "limits"))


def run_one(c, execute, default_timeout, awaiter):
    """(rc, output, why, values) of one command."""
    aw = c.get("await")
    if aw is not None and awaiter is None:
        return None, "", ["await needs a board's console log (its broker's), which this route has not"], {}
    start = awaiter.offset() if aw is not None else None
    rc, output = execute(c["cmd"], float(c.get("timeout") or default_timeout))
    match, why = None, []
    if aw is not None:
        pattern = re.compile(aw["pattern"], re.M)
        within = float(aw.get("within", AWAIT_WITHIN))
        output, match = awaiter.wait(start, c["cmd"], pattern, within)
        if match is None:
            why.append(f"no line matching /{aw['pattern']}/ within {within:g} s")
    why += judge(c, rc, output)
    if match is None and aw is None and c.get("expect") is not None:
        match = re.search(c["expect"], output, re.M)
    values = captured(match)
    if c.get("limits") and match is not None:
        why += judge_limits(c["limits"], values)
    return rc, output, why, values


def run_all(commands, execute, default_timeout=20.0, awaiter=None, stop_on_failure=False):
    """[{name, cmd, output, rc, test, passed, why, values?}] in order. A command that cannot be
    run at all (the transport raises) fails with the transport's reason and the run goes on;
    with stop_on_failure, the commands after a failed one are reported as not run."""
    check(commands)
    results = []
    for c in commands:
        entry = {"name": c["name"], "cmd": c["cmd"], "test": is_test(c)}
        if stop_on_failure and any(not r["passed"] for r in results):
            entry.update(rc=None, output="", passed=False, why=["not run: a command before it failed"])
            results.append(entry)
            continue
        try:
            rc, output, why, values = run_one(c, execute, default_timeout, awaiter)
        except Exception as e:  # pylint: disable=broad-except
            rc, output, why, values = None, "", [f"did not run: {e}"], {}
        entry.update(rc=rc, output=output, passed=not why, why=why)
        if values:
            entry["values"] = values
        results.append(entry)
    return results
