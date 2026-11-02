"""A reboot through the console: what counts as proof that it happened, and who is refused."""
import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, reboot
from ansible_collections.rxrbench.core.plugins.plugin_utils.families import PLUGINS


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s


class Board:
    """A Zephyr shell that was up `up` seconds. `reboots`: the reboot command restarts it;
    `down`: answers lost to the closed console after it; `closes`: the command itself fails."""
    board = "b1"

    def __init__(self, clock, up=500.0, reboots=True, down=0, closes=False):
        self.clock, self.booted = clock, clock.now - up
        self.reboots, self.down, self.closes = reboots, down, closes
        self.sent = []

    def exec(self, cmd, deadline=8.0, **kw):
        self.sent.append(cmd)
        if cmd == "kernel reboot cold":
            if self.reboots:
                self.booted = self.clock.now
            if self.closes:
                raise broker_client.BrokerError("b1: port closed")
            return ""
        if self.down and "kernel reboot cold" in self.sent[:-1]:
            self.down -= 1
            raise broker_client.BrokerError("b1: port closed")
        return f"Uptime: {int((self.clock.now - self.booted) * 1000)} ms"


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(reboot, "time", c)
    return c


def test_a_console_that_went_down_and_came_back_is_a_reboot(widgets, clock):
    b = Board(clock, down=4, closes=True)
    r = reboot.reboot(b, "widget")
    assert r["before_s"] == 500.0 and r["after_s"] < 5 and r["took_s"] == 2.0


def test_a_console_that_stayed_open_is_a_reboot_by_its_uptime(widgets, clock):
    r = reboot.reboot(Board(clock), "widget")
    assert r["after_s"] == 0.0


def test_a_board_that_kept_running_is_not_called_rebooted(widgets, clock):
    b = Board(clock, reboots=False)
    with pytest.raises(reboot.RebootError, match="no reboot seen within 30 s: the board kept"):
        reboot.reboot(b, "widget", settle=30)
    assert b.sent.count("kernel reboot cold") == 1, "the command is sent once"


def test_a_board_that_never_answers_again_says_why(widgets, clock):
    b = Board(clock, down=10**6, closes=True)
    with pytest.raises(reboot.RebootError, match="port closed"):
        reboot.reboot(b, "widget", settle=10)


def test_a_just_booted_board_is_still_told_apart(widgets, clock):
    """Up 1 s before: after the reboot its uptime is not 'less than before' for long, so the
    comparison is with what it would be had it kept running."""
    b = Board(clock, up=1.0, down=6, closes=True)
    assert reboot.reboot(b, "widget")["after_s"] == 3.0


def test_an_answer_that_is_no_uptime_is_an_error(widgets):
    class Odd:
        def exec(self, cmd, deadline=8.0):
            return "kernel: command not found"
    with pytest.raises(reboot.RebootError, match="not an uptime"):
        reboot.uptime(Odd(), "widget")


@pytest.mark.plugins
@pytest.mark.parametrize("family, why", [
    ("meshtastic", "no console"), ("bare", "no console"),
    ("some-vendor-firmware", "no console"),
    ("launcher", "does not say how long it has been up"),
])
def test_a_family_that_cannot_be_asked_is_refused(family, why):
    with pytest.raises(reboot.RebootError, match=why):
        reboot.commands(family)


@pytest.mark.plugins
def test_the_families_with_a_console_and_an_uptime_have_commands():
    assert reboot.commands("zephyrtastic")["reboot"] == "kernel reboot cold"
    assert reboot.commands("zephcore")["reboot"] == "reboot"
    # no uptime, but a boot banner and a command it answers once back
    assert reboot.commands("meshcore")["ready"] == "ver"
    assert PLUGINS["zephyrtastic"].reset_command == "meshtastic resets"
    assert PLUGINS["zephcore"].reset_command is None


@pytest.mark.plugins
def test_a_linux_device_is_rebooted_over_ssh_and_proven_by_its_uptime():
    """The same proof as on a console: it went down and came back, or its uptime fell."""
    from ansible_collections.rxrbench.core.plugins.module_utils import ssh

    class Target:
        host = "192.0.2.9"

        def __init__(self):
            self.said, self.ups = [], iter([(0, "5000.11 900.0\n"), (255, "connection refused"),
                                            (0, "4.20 1.0\n")])

        def run(self, cmd, timeout=30):
            self.said.append(cmd)
            return (0, "") if "reboot" in cmd else next(self.ups)

    for family in ("linux", "linux_rauc", "linux_slotguard"):
        t = Target()
        r = reboot.reboot(ssh.Console(t), family, settle=30)
        assert r["before_s"] == 5000 and r["after_s"] == 4, family
        assert any("reboot" in c for c in t.said)
    with pytest.raises(ssh.SshError, match="192.0.2.9"):
        ssh.Console(type("Down", (), {"host": "192.0.2.9", "run": lambda s, c, timeout=1: (255, "")})()).exec("true")


class ReopeningBoard(Board):
    """Behind a broker that reopens a closed port for the next command (29ba939): no read fails
    while the board is away; only the broker's count of port opens shows it went down."""

    def __init__(self, clock, **kw):
        super().__init__(clock, **kw)
        self.opens = 1

    def status(self):
        return {"ok": True, "opens": self.opens}

    def exec(self, cmd, deadline=8.0, **kw):
        if cmd == "kernel reboot cold" and self.reboots:
            self.opens += 1
        return super().exec(cmd, deadline, **kw)


def test_a_just_booted_board_whose_port_the_broker_reopened_is_a_reboot(widgets, clock):
    """A board up 1 s, no failed read in between, so neither
    a closed console nor its uptime could tell; the broker's opens count does."""
    r = reboot.reboot(ReopeningBoard(clock, up=1.0), "widget")
    assert r["after_s"] == 0.0 and r["took_s"] == 0.0


def test_a_board_that_kept_running_behind_a_broker_is_still_not_called_rebooted(widgets, clock):
    b = ReopeningBoard(clock, up=1.0, reboots=False)
    with pytest.raises(reboot.RebootError, match="kept running"):
        reboot.reboot(b, "widget", settle=10)


def test_opens_is_none_for_a_board_that_cannot_say():
    assert broker_client.opens(broker_client.NoBroker("b1", "/nonexistent")) is None
    assert broker_client.opens(object()) is None
    assert broker_client.reopened(object(), 3) is False
    assert broker_client.reopened(object(), None) is False
