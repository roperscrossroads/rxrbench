"""Run named commands on a device and judge their output: what diagnostics and tests share.

A command is `{name, cmd}` plus, for a test, what its output must contain (`expect`, a
regular expression), must not contain (`refuse`), and for a device with exit codes the code
it must return (`rc`, default 0). A command with none of those is a diagnostic: its output
is collected and it cannot fail, except by not running at all.

`run_all()` takes the transport as a function `execute(cmd, timeout) -> (rc or None, output)`,
so the same judging serves a board's shell (no exit codes) and a Linux device over ssh.
"""
import re

KEYS = {"name", "cmd", "expect", "refuse", "rc", "timeout"}


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
    return any(command.get(k) is not None for k in ("expect", "refuse", "rc"))


def run_all(commands, execute, default_timeout=20.0):
    """[{name, cmd, output, rc, test, passed, why}] in order. A command that cannot be run
    at all (the transport raises) fails with the transport's reason and the run goes on."""
    check(commands)
    results = []
    for c in commands:
        entry = {"name": c["name"], "cmd": c["cmd"], "test": is_test(c)}
        try:
            rc, output = execute(c["cmd"], float(c.get("timeout") or default_timeout))
            why = judge(c, rc, output)
        except Exception as e:  # pylint: disable=broad-except
            rc, output, why = None, "", [f"did not run: {e}"]
        entry.update(rc=rc, output=output, passed=not why, why=why)
        results.append(entry)
    return results
