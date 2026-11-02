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
