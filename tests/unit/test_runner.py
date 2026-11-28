import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import runner


def shell(answers):
    """A board's shell: output only, no exit code."""
    return lambda cmd, timeout: (None, answers[cmd])


def test_a_diagnostic_is_collected_and_cannot_fail_on_its_output():
    r = runner.run_all([{"name": "version", "cmd": "meshtastic version"}],
                       shell({"meshtastic version": "build: abc\nclass:  4"}))
    assert r == [{"name": "version", "cmd": "meshtastic version", "test": False, "rc": None,
                  "output": "build: abc\nclass:  4", "passed": True, "why": []}]


def test_a_test_passes_or_says_why_not():
    cmds = [{"name": "class", "cmd": "v", "expect": r"^class:\s+4$"},
            {"name": "quiet", "cmd": "v", "refuse": "WATCHDOG"},
            {"name": "wrong", "cmd": "v", "expect": r"^class:\s+6$"},
            {"name": "noisy", "cmd": "v", "refuse": "build"}]
    r = {x["name"]: x for x in runner.run_all(cmds, shell({"v": "build: abc\nclass:  4"}))}
    assert r["class"]["passed"] and r["quiet"]["passed"]
    assert r["wrong"]["why"] == [r"output does not match /^class:\s+6$/"]
    assert r["noisy"]["why"] == ["output matches /build/"]


def test_exit_codes_count_where_the_device_has_them():
    run = lambda cmd, timeout: {"ok": (0, "fine"), "bad": (3, "nope")}[cmd]  # noqa: E731
    r = runner.run_all([{"name": "a", "cmd": "ok", "expect": "fine"},
                        {"name": "b", "cmd": "bad", "expect": "nope"},
                        {"name": "c", "cmd": "bad", "rc": 3},
                        {"name": "d", "cmd": "bad"}], run)
    assert [x["passed"] for x in r] == [True, False, True, True]
    assert r[1]["why"] == ["exit code 3, wanted 0"]
    assert r[3]["test"] is False, "a diagnostic's exit code is recorded, not judged"


def test_a_command_that_cannot_run_fails_and_the_rest_still_run():
    def run(cmd, timeout):
        if cmd == "dead":
            raise OSError("the broker closed the connection")
        return None, "ok"
    r = runner.run_all([{"name": "a", "cmd": "dead"}, {"name": "b", "cmd": "alive"}], run)
    assert r[0]["passed"] is False and "did not run: the broker closed" in r[0]["why"][0]
    assert r[1]["passed"] is True


@pytest.mark.parametrize("bad, why", [
    ([{"name": "a"}], "needs name and cmd"),
    ([{"name": "a", "cmd": "x", "colour": "red"}], "needs name and cmd"),
    ([{"name": "a b", "cmd": "x"}], "becomes a file name"),
    ([{"name": "a", "cmd": "x"}, {"name": "a", "cmd": "y"}], "named twice"),
    ([{"name": "a", "cmd": "x", "expect": "("}], "not a regular expression"),
])
def test_a_command_list_that_makes_no_sense_is_refused_before_anything_runs(bad, why):
    ran = []
    with pytest.raises(runner.RunnerError, match=why):
        runner.run_all(bad, lambda cmd, t: ran.append(cmd) or (None, ""))
    assert not ran


class Log:
    """A console log a command's later lines appear in (consolelog.ConsoleLog's interface)."""

    def __init__(self, later):
        self.later, self.text = later, ""

    def offset(self):
        return len(self.text)

    def wait(self, start, cmd, pattern, within):
        self.text += f"uart:~$ {cmd}\n" + self.later.get(cmd, "")
        body = self.text[start:].split("\n", 1)[1]
        m = pattern.search(body)
        return (body[:body.find("\n", m.end())] if m and "\n" in body[m.end():] else body), m


SUMMARY = r"^tool sh: rc (?P<rc>-?\d+), (?P<ms>\d+) ms, heap peak (?P<heap>\d+) B"


def test_await_sends_once_reads_the_later_line_and_judges_what_it_captures():
    sent = []
    run = lambda cmd, timeout: sent.append(cmd) or (None, "")  # noqa: E731
    log = Log({"tool run sh": "sh: 3 threads\ntool sh: rc 0, 21 ms, heap peak 3472 B\nnext\n"})
    cmds = [{"name": "ok", "cmd": "tool run sh", "await": {"pattern": SUMMARY, "within": 5},
             "limits": {"rc": {"eq": 0}, "heap": {"max": 4096}, "ms": {"min": 1, "max": "100"}}}]
    [r] = runner.run_all(cmds, run, awaiter=log)
    assert sent == ["tool run sh"]
    assert r["passed"] and r["values"] == {"rc": 0, "ms": 21, "heap": 3472}, r
    assert r["output"] == "sh: 3 threads\ntool sh: rc 0, 21 ms, heap peak 3472 B", r


def test_a_limit_broken_and_a_line_that_never_came_say_why():
    run = lambda cmd, timeout: (None, "")  # noqa: E731
    log = Log({"tool run sh": "tool sh: rc -5, 21 ms, heap peak 9000 B\n"})
    cmds = [{"name": "big", "cmd": "tool run sh", "await": {"pattern": SUMMARY},
             "limits": {"rc": {"eq": 0}, "heap": {"max": 4096}}},
            {"name": "never", "cmd": "tool run other", "await": {"pattern": SUMMARY, "within": 1}}]
    r = {x["name"]: x for x in runner.run_all(cmds, run, awaiter=log)}
    assert r["big"]["why"] == ["rc -5, wanted 0", "heap 9000, over 4096"], r
    assert r["never"]["why"] == [f"no line matching /{SUMMARY}/ within 1 s"], r


def test_await_without_a_console_log_is_refused_not_passed():
    [r] = runner.run_all([{"name": "a", "cmd": "x", "await": {"pattern": "done"}}],
                         lambda cmd, t: (0, "done"))
    assert not r["passed"] and "console log" in r["why"][0], r


def test_limits_judge_expects_captures_without_await():
    [r] = runner.run_all([{"name": "up", "cmd": "kernel uptime",
                           "expect": r"Uptime: (?P<ms>\d+) ms", "limits": {"ms": {"min": 5000}}}],
                         shell({"kernel uptime": "Uptime: 1200 ms"}))
    assert r["values"] == {"ms": 1200} and r["why"] == ["ms 1200, under 5000"], r


@pytest.mark.parametrize("cmd, says", [
    ({"await": {"within": 3}}, "await is {pattern, within}"),
    ({"await": {"pattern": "(", "within": 3}}, "await's pattern is not a regular expression"),
    ({"await": {"pattern": "x", "within": 0}}, "above 0"),
    ({"limits": {"rc": {"eq": 0}}}, "on the groups await's pattern (or expect) captures"),
    ({"expect": "(?P<rc>\\d+)", "limits": {"heap": {"max": 1}}}, "does not capture (it captures: rc)"),
    ({"expect": "(?P<rc>\\d+)", "limits": {"rc": {"below": 1}}}, "limits.rc is {min, max, eq}"),
    ({"expect": "(?P<rc>\\d+)", "limits": {"rc": {"max": "lots"}}}, "limits.rc.max is a number"),
])
def test_a_malformed_await_or_limit_is_refused_before_anything_runs(cmd, says):
    with pytest.raises(runner.RunnerError, match=None) as e:
        runner.check([{"name": "a", "cmd": "x", **cmd}])
    assert says in str(e.value), str(e.value)


def test_stop_on_failure_runs_nothing_after_a_failed_command():
    sent = []
    run = lambda cmd, timeout: sent.append(cmd) or (None, {"a": "ok", "b": "bad", "c": "ok"}[cmd])  # noqa: E731
    cmds = [{"name": n, "cmd": n, "expect": "^ok"} for n in "abc"]
    r = runner.run_all(cmds, run, stop_on_failure=True)
    assert sent == ["a", "b"] and [x["passed"] for x in r] == [True, False, False], r
    assert r[2]["why"] == ["not run: a command before it failed"]
    assert len(runner.run_all(cmds, run)) == 3 and sent[-1] == "c"
