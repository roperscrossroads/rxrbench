import importlib.util
import os
import threading
import time

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from tests.support.fake_shell import FakeShell

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("rxrbench_broker",
                                              os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "broker.py"))
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)

VERSION = "build:  0.4.37-abc (built 1002-1140 UTC)\nimage:  0.4.37+0 (MCUboot header, slot0)\nclass:  4"
LONG = "meshtastic channel set 2 name cluster role secondary psk hex " + "ab" * 32


@pytest.fixture
def rig(tmp_path):
    made = []

    def make(commands=None, **kw):
        shell = FakeShell(commands, **kw)
        console = mb.Console("sim1", mb.path_opener(shell.path), str(tmp_path / "sim1.log"))
        broker = mb.Broker(console, str(tmp_path / "run" / "sim1.sock"))
        t = threading.Thread(target=broker.run, daemon=True)
        t.start()
        made.append((shell, broker, t))
        end = time.time() + 3
        while not console.is_open() and time.time() < end:
            time.sleep(0.01)
        return shell, broker_client.Board("sim1", path=broker.sock_path, timeout=20)

    yield make
    for shell, broker, t in made:
        broker.shutdown()
        t.join(timeout=2)
        shell.close()


def test_a_command_returns_exactly_its_output(rig):
    shell, board = rig({"meshtastic version": VERSION})
    with board as b:
        assert b.exec("meshtastic version") == VERSION
        assert b.exec("meshtastic version") == VERSION  # and again: nothing left over


def test_a_log_line_in_the_middle_does_not_end_the_read_early(rig):
    def noisy(sh):
        sh.out("first half\r\n")
        sh.log_line()   # redraws the prompt: "the next prompt" would stop here
        sh.out("\r\n")
        return "second half"
    shell, board = rig({"meshtastic rf": noisy})
    with board as b:
        out = b.exec("meshtastic rf")
    assert out.startswith("first half") and out.endswith("second half")
    assert "<inf> meshtastic: RX from" in out   # the log line is kept, not silently dropped


def test_a_command_longer_than_the_ring_arrives_whole(rig):
    shell, board = rig({LONG: "channel 2 updated (hash=0xe0)"}, ring=64)
    assert len(LONG) > 64
    with board as b:
        assert b.exec(LONG) == "channel 2 updated (hash=0xe0)"
    assert shell.dropped == 0 and LONG in shell.seen


def test_an_unknown_command_is_output_not_a_hang(rig):
    shell, board = rig()
    with board as b:
        assert b.exec("meshtastic nope") == "meshtastic: command not found"


def test_a_slow_command_is_waited_for(rig):
    def slow(sh):
        time.sleep(1.2)
        return "done at last"
    shell, board = rig({"meshtastic slow": slow})
    with board as b:
        assert b.exec("meshtastic slow", deadline=5) == "done at last"


def test_a_command_that_never_returns_times_out_and_says_so(rig):
    def hang(sh):
        sh.out("partial\r\n")
        time.sleep(3)
    shell, board = rig({"meshtastic hang": hang})
    with board as b:
        r = b.request(op="exec", cmd="meshtastic hang", deadline=1.0)
    assert r == {"ok": False, "error": "timeout", "output": "partial", "elapsed": r["elapsed"]}


def test_a_dead_shell_is_no_echo_not_a_timeout(rig):
    shell, board = rig(dead=True)
    with board as b, pytest.raises(broker_client.BrokerError, match="no echo"):
        b.exec("meshtastic version")


def test_everything_the_board_says_is_logged(rig, tmp_path):
    shell, board = rig({"meshtastic version": VERSION})
    with board as b:
        b.exec("meshtastic version")
    shell.out("\r\nunsolicited boot banner\r\n")
    time.sleep(0.6)
    log = (tmp_path / "sim1.log").read_text(errors="replace")
    assert "class:  4" in log and "unsolicited boot banner" in log and "### rxrbench" in log


def test_release_closes_the_port_and_reacquire_reopens_it(rig):
    shell, board = rig({"meshtastic version": VERSION})
    with board as b:
        assert b.release(ttl=60) == shell.path
        st = b.status()
        assert st["open"] is False and st["released_for"] > 50
        with pytest.raises(broker_client.BrokerError, match="port closed"):
            b.exec("meshtastic version")
        assert b.reacquire()["ok"] is True
        assert b.exec("meshtastic version") == VERSION


def test_a_release_expires_by_itself(rig):
    shell, board = rig({"meshtastic version": VERSION})
    with board as b:
        b.release(ttl=1)
        time.sleep(1.8)
        assert b.status()["open"] is True


def test_a_second_client_is_told_busy(rig):
    shell, board = rig({"meshtastic version": VERSION})
    with board as b:
        b.status()
        other = broker_client.Board("sim1", path=board.path, timeout=5)
        with other as o:
            import json
            assert json.loads(o.sock.recv(4096))["error"] == "busy"


def test_a_multi_line_command_is_refused(rig):
    shell, board = rig()
    with board as b, pytest.raises(broker_client.BrokerError, match="one non-empty line"):
        b.exec("a\rb")


def test_a_stopped_broker_leaves_no_socket_behind(tmp_path):
    # systemd stops the unit with SIGTERM. A socket file left behind would tell the old flash
    # tools that the board is still owned (they refuse it) when nothing holds the port.
    import signal
    import subprocess
    import sys
    shell = FakeShell({"meshtastic version": VERSION})
    sock = tmp_path / "run" / "sim1.sock"
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "broker.py"),
                             "--board", "sim1", "--port", shell.path, "--socket", str(sock),
                             "--log", str(tmp_path / "sim1.log")])
    try:
        end = time.time() + 5
        while not sock.exists() and time.time() < end:
            time.sleep(0.02)
        assert sock.exists()
        with broker_client.Board("sim1", path=str(sock), timeout=10) as b:
            assert b.exec("meshtastic version") == VERSION
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
        assert not sock.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        shell.close()


def test_the_log_rotates_at_its_cap_and_loses_no_byte(tmp_path):
    path = str(tmp_path / "b.log")
    log = mb.RotatingLog(path, max_bytes=100, keep=2)
    data = bytes(range(256)) * 2            # 512 B in chunks of 40
    for i in range(0, len(data), 40):
        log.write(data[i:i + 40])
    files = [path + ".2", path + ".1", path]
    assert all(os.path.getsize(f) <= 100 for f in files)
    assert not os.path.exists(path + ".3")
    kept = b"".join(open(f, "rb").read() for f in files)
    assert data.endswith(kept)               # the newest bytes, in order, none lost


def test_a_cap_of_zero_never_rotates(tmp_path):
    log = mb.RotatingLog(str(tmp_path / "b.log"), max_bytes=0, keep=2)
    for _ in range(50):
        log.write(b"x" * 100)
    assert os.path.getsize(tmp_path / "b.log") == 5000


def test_brokerctl_runs_each_command_and_reports_a_timeout_as_not_ok(rig, capsys):
    import json as _json
    spec2 = importlib.util.spec_from_file_location("brokerctl", os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "brokerctl.py"))
    ctl = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(ctl)

    def slow(sh):
        time.sleep(3)
        return "late"
    shell, board = rig({"meshtastic version": VERSION, "meshtastic slow": slow})
    sock_dir = os.path.dirname(board.path)
    rc = ctl.main(["--socket-dir", sock_dir, "exec", "sim1", "meshtastic version", "meshtastic slow",
                   "--deadline", "1"])
    lines = [_json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert rc == 1
    assert lines[0]["ok"] is True and lines[0]["output"] == VERSION
    assert lines[1]["ok"] is False and lines[1]["error"] == "timeout"
    assert ctl.main(["--socket-dir", str(sock_dir) + "-nowhere", "exec", "sim1", "x"]) == 2


def test_brokerctl_releases_the_port_and_takes_it_back(rig, capsys):
    spec2 = importlib.util.spec_from_file_location("brokerctl2", os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "brokerctl.py"))
    ctl = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(ctl)
    shell, board = rig({"meshtastic version": VERSION})
    d = os.path.dirname(board.path)
    assert ctl.main(["--socket-dir", d, "release", "sim1", "--ttl", "60"]) == 0
    assert capsys.readouterr().out.strip() == shell.path
    with board as b:
        assert b.status()["open"] is False
    assert ctl.main(["--socket-dir", d, "reacquire", "sim1", "--wait", "5"]) == 0
    capsys.readouterr()
    with board as b:
        assert b.exec("meshtastic version") == VERSION


def test_a_mark_goes_into_the_log_through_the_broker(rig, tmp_path):
    shell, board = rig({"meshtastic version": VERSION})
    spec3 = importlib.util.spec_from_file_location("brokerctl3", os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "brokerctl.py"))
    ctl = importlib.util.module_from_spec(spec3)
    spec3.loader.exec_module(ctl)
    d = os.path.dirname(board.path)
    assert ctl.main(["--socket-dir", d, "mark", "sim1", "FLASHED", "0.4.41"]) == 0
    assert ctl.main(["--socket-dir", d, "mark", "sim1", ""]) == 1   # an empty mark is refused
    log = open(tmp_path / "sim1.log", "rb").read().decode()
    assert any(line.startswith("### rxrbench ") and line.endswith("sim1: FLASHED 0.4.41")
               for line in log.splitlines())


# ---- the MeshCore console dialect (zephcore, stock MeshCore) ---------------------------

@pytest.fixture
def mc_rig(tmp_path):
    from tests.support.fake_meshcore import FakeMeshCore
    made = []

    def make(**kw):
        node = FakeMeshCore(**kw)
        log = str(tmp_path / "mc1.log")
        console = mb.Console("mc1", mb.path_opener(node.path), log)
        broker = mb.Broker(console, str(tmp_path / "run" / "mc1.sock"))
        t = threading.Thread(target=broker.run, daemon=True)
        t.start()
        made.append((node, broker, t))
        end = time.time() + 3
        while not console.is_open() and time.time() < end:
            time.sleep(0.01)
        return node, broker_client.Board("mc1", path=broker.sock_path, timeout=20), log

    yield make
    for node, broker, t in made:
        broker.shutdown()
        t.join(timeout=2)
        node.close()


@pytest.mark.plugins
def test_meshcore_answers_are_one_line_each(mc_rig):
    """The mesh plugin's dialect file (meshcore.json) reads zephcore's answers."""
    node, board, _log = mc_rig()
    with board as b:
        assert b.exec("get name", dialect="meshcore") == "> zephcore"
        assert b.exec("foo", dialect="meshcore") == "Unknown command"
        assert b.exec("zephcore state", dialect="meshcore").startswith('~S{"v":1')
        assert b.exec("zephcore tx", dialect="meshcore") == "tx off"


# ---- a line dialect from a plugin's dialects.d (the fixture plugin's `widget-line`) ------

FIXTURE_DIALECTS = os.path.join(REPO, "tests", "fixtures", "collections", "ansible_collections",
                                "fixture", "widgets", "host", "dialects.d")


@pytest.fixture
def widget_dialect():
    """The fixture plugin's console rules loaded into this broker, as the role deploys a
    plugin's dialects.d on a host; taken out again after the test."""
    dialects, scrubs = dict(mb.DIALECTS), list(mb.SCRUBS)
    mb.load_dialects(FIXTURE_DIALECTS)
    yield mb.DIALECTS["widget-line"]
    mb.DIALECTS.clear()
    mb.DIALECTS.update(dialects)
    mb.SCRUBS[:] = scrubs


@pytest.fixture
def line_rig(tmp_path, widget_dialect):
    from tests.support.fake_widget_console import FakeWidgetConsole
    made = []

    def make(**kw):
        node = FakeWidgetConsole(**kw)
        log = str(tmp_path / "w1.log")
        console = mb.Console("w1", mb.path_opener(node.path), log)
        broker = mb.Broker(console, str(tmp_path / "run" / "w1.sock"))
        t = threading.Thread(target=broker.run, daemon=True)
        t.start()
        made.append((node, broker, t))
        end = time.time() + 3
        while not console.is_open() and time.time() < end:
            time.sleep(0.01)
        return node, broker_client.Board("w1", path=broker.sock_path, timeout=20), log

    yield make
    for node, broker, t in made:
        broker.shutdown()
        t.join(timeout=2)
        node.close()


def test_a_line_dialect_answers_are_one_line_each(line_rig):
    node, board, _log = line_rig()
    with board as b:
        assert b.exec("get name", dialect="widget-line") == "widget1"
        assert b.exec("foo", dialect="widget-line") == "Unknown command"
        assert b.exec("widget state", dialect="widget-line").startswith('~S{"v":1')


def test_a_password_never_reaches_the_console_log(line_rig):
    node, board, log = line_rig()
    with board as b:
        assert b.exec("passwd correct-horse-9", dialect="widget-line") == "OK"
    assert node.st["password"] == "correct-horse-9"
    assert "correct-horse-9" not in open(log, "rb").read().decode(errors="replace")


def test_a_board_that_repeats_a_password_does_not_get_it_into_the_log_or_the_answer(line_rig):
    # The contract is that the firmware does not echo a secret. This one echoes it and says it
    # again in its answer: the broker was told what the secret is, and keeps it out of both.
    node, board, log = line_rig(echo_secrets=True)
    with board as b:
        assert b.exec("passwd correct-horse-9", dialect="widget-line") == "OK passwd is <secret>"
        assert b.exec("get name", dialect="widget-line") == "widget1"
    assert node.st["password"] == "correct-horse-9"
    time.sleep(1.2)                       # a held partial line is let out after a second
    text = open(log, "rb").read().decode(errors="replace")
    assert "correct-horse-9" not in text and "passwd <secret>" in text


def test_a_secret_named_by_the_request_is_kept_out_on_a_plain_shell_too(rig, tmp_path):
    shell, board = rig({"keyset s3cr3t-value-77": "stored s3cr3t-value-77"})
    with board as b:
        assert b.exec("keyset s3cr3t-value-77", secret="s3cr3t-value-77") == "stored <secret>"
    time.sleep(1.2)
    assert b"s3cr3t-value-77" not in open(tmp_path / "sim1.log", "rb").read()


def test_a_named_secret_is_forgotten_after_its_time():
    out = []
    log = mb.ScrubbedLog(type("L", (), {"write": lambda self, d: out.append(d)})())
    log.hide("hunter22x", ttl=0.05)
    log.write(b"a hunter22x b\n")
    time.sleep(0.1)
    log.write(b"c hunter22x d\n")
    assert out == [b"a <secret> b\n", b"c hunter22x d\n"] and not log.secrets


def test_a_reboot_without_an_answer_is_not_a_timeout(line_rig):
    node, board, _log = line_rig()
    with board as b:
        said = b.exec("widget wipe", deadline=1, dialect="widget-line", expect_reply=False)
    # no answer, no timeout: what the board printed meanwhile (here its echo) comes back
    assert "= " not in said and said.strip() == "widget wipe"
    assert node.wipes == 1


def test_a_board_that_stops_reading_closes_the_port_not_the_broker(tmp_path):
    import pty
    master, slave = pty.openpty()               # the board's end is never read
    console = mb.Console("deaf1", mb.path_opener(os.ttyname(slave)), str(tmp_path / "d.log"))
    console.open()
    done = {}

    def write():
        try:
            for _ in range(256):
                console.write(b"x" * 4096)
            done["r"] = "wrote it all"
        except mb.PortError as e:
            done["r"] = str(e)

    t = threading.Thread(target=write, daemon=True)
    t.start()
    t.join(timeout=mb.WRITE_TIMEOUT + 5)
    os.close(master)
    os.close(slave)
    assert not t.is_alive(), "a write to a console nobody reads must not hang"
    assert done["r"].startswith("port closed") and not console.is_open()


class _Sink:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


FRAME = "Cg" + "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=" * 3


@pytest.mark.plugins
def test_tunnel_payloads_never_reach_the_log_even_split_across_reads():
    """The mesh plugin's dialect file (meshtastic.json) keeps the phone API frames out."""
    sink = _Sink()
    log = mb.ScrubbedLog(sink)
    text = (f"uart:~$ meshtastic api + {FRAME}\r\n~P{FRAME}*1a2b\r\n~P.1\r\n"
            f"<inf> meshtastic: ok\r\nuart:~$ ").encode()
    for i in range(0, len(text), 7):                # arrives in small pieces
        log.write(text[i:i + 7])
    log.flush()
    assert FRAME[:20].encode() not in sink.data
    assert b"~P<frame, " in sink.data and b"meshtastic api + <" in sink.data
    assert b"~P.1" in sink.data and b"<inf> meshtastic: ok" in sink.data
    assert sink.data.endswith(b"uart:~$ "), "a prompt is let out by flush"


def test_a_plugins_log_rules_hold_even_split_across_reads(widget_dialect):
    sink = _Sink()
    log = mb.ScrubbedLog(sink)
    text = (f"uart:~$ widget tunnel + {FRAME}\r\n~W{FRAME}*1a2b\r\n~W.1\r\n"
            f"<inf> widget: ok\r\nuart:~$ ").encode()
    for i in range(0, len(text), 7):                # arrives in small pieces
        log.write(text[i:i + 7])
    log.flush()
    assert FRAME[:20].encode() not in sink.data
    assert b"~W<frame, " in sink.data and b"widget tunnel + <" in sink.data
    assert b"~W.1" in sink.data and b"<inf> widget: ok" in sink.data
    assert sink.data.endswith(b"uart:~$ "), "a prompt is let out by flush"


def test_a_held_prompt_waits_at_most_its_second():
    sink = _Sink()
    log = mb.ScrubbedLog(sink)
    log.write(b"uart:~$ ")
    log.flush(older_than=1.0)
    assert sink.data == b""
    log.since -= 1.5
    log.flush(older_than=1.0)
    assert sink.data == b"uart:~$ "


def test_the_client_still_gets_every_byte_of_a_frame(rig):
    frame = f"~P{FRAME}*1a2b"
    shell, b = rig({"meshtastic api": frame})
    with b:
        assert frame in b.exec("meshtastic api")


def test_an_error_never_quotes_a_secret_bearing_command():
    payload = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVph"
    assert broker_client.shown(f"meshtastic api {payload} b8b3") == "meshtastic api <36 chars>"
    assert broker_client.shown(f"meshtastic api + {payload}") == "meshtastic api + <36 chars>"
    assert broker_client.shown("password hunter2-xyz") == "password <secret>"
    assert broker_client.shown("set prv.key " + "ab" * 64) == "set prv.key <secret>"
    assert broker_client.shown("meshtastic version") == "meshtastic version"


def test_a_second_client_waits_its_turn_instead_of_failing(rig):
    shell, board = rig({"meshtastic version": VERSION})

    def hold():
        with board as b:
            b.status()
            time.sleep(1.0)

    t = threading.Thread(target=hold)
    t.start()
    time.sleep(0.3)
    started = time.monotonic()
    with broker_client.Board("sim1", path=board.path, timeout=5, busy_wait=10) as o:
        assert o.exec("meshtastic version") == VERSION
    assert time.monotonic() - started >= 0.4, "it waited for the first client"
    t.join()


def test_a_client_that_waits_too_long_is_told_busy(rig):
    shell, board = rig({"meshtastic version": VERSION})
    with board as b:
        b.status()
        with broker_client.Board("sim1", path=board.path, timeout=5, busy_wait=0.5) as o:
            assert o.status() == {"ok": False, "error": "busy"}


# ---- the broker's hold on a real board's port: a device lock and an exclusive open ------------

def devreg_stub(tmp_path, port):
    """A devreg that resolves and guards one port, with the real devlock beside it."""
    import shutil
    d = tmp_path / "devreg"
    d.mkdir()
    shutil.copy(os.path.join(REPO, "devreg", "devlock.py"), d / "devlock.py")
    (d / "devreg.py").write_text(f"import sys\nprint({port!r}) if sys.argv[1] == 'resolve' else None\n")
    return str(d / "devreg.py")


def test_the_broker_holds_the_ports_device_lock_and_opens_it_exclusively(tmp_path, monkeypatch):
    import sys
    monkeypatch.setenv("DEVLOCK_DIR", str(tmp_path / "locks"))
    shell = FakeShell()
    console = mb.Console("b1", mb.devreg_opener("b1", devreg_stub(tmp_path, shell.path)),
                         str(tmp_path / "b1.log"))
    console.open()
    devlock = sys.modules["devlock"]
    try:
        held = devlock.probe(shell.path)
        assert held and held["tool"] == "rxrbench-broker" and held["device"] == "b1"
        assert console.ser.exclusive is True
        # A second broker (or any tool that asks devlock) is told who has the port.
        with pytest.raises(mb.PortError, match="in use: rxrbench-broker"):
            mb.devreg_opener("b1", os.path.join(str(tmp_path), "devreg", "devreg.py"))()
        # A release frees the port and the lock together, for the flasher that asked.
        console.close("released")
        assert devlock.probe(shell.path) is None
        console.open()
        assert devlock.probe(shell.path)["tool"] == "rxrbench-broker"
    finally:
        console.close("test over")
        shell.close()


def test_a_port_someone_else_holds_is_not_opened(tmp_path, monkeypatch):
    import sys
    monkeypatch.setenv("DEVLOCK_DIR", str(tmp_path / "locks"))
    shell = FakeShell()
    opener = mb.devreg_opener("b1", devreg_stub(tmp_path, shell.path))
    devlock = __import__("devlock")
    try:
        with devlock.DevLock(shell.path, tool="a-flasher"):
            with pytest.raises(mb.PortError, match="in use: a-flasher"):
                opener()
        port, ser, lock = opener()
        ser.close()
        lock.release()
    finally:
        shell.close()
        sys.modules.pop("devlock", None)


def test_a_clients_deadline_is_bounded_by_the_broker(tmp_path, widget_dialect):
    class Quiet:
        seen = None
        log = type("L", (), {"hide": lambda self, secret: None})()

        def exec(self, cmd, deadline=8.0):
            self.seen = deadline
            return {"ok": True, "output": ""}

        def exec_line(self, cmd, dialect, deadline=8.0, expect_reply=True):
            self.seen = deadline
            return {"ok": True, "output": ""}

        def close(self, why):
            pass

    console = Quiet()
    broker = mb.Broker(console, str(tmp_path / "run" / "q.sock"))
    try:
        for asked, got in ((1e9, mb.MAX_DEADLINE), (-5, 0.1), (12, 12.0)):
            broker.handle({"op": "exec", "cmd": "x", "deadline": asked})
            assert console.seen == got
        broker.handle({"op": "exec", "cmd": "x", "deadline": 1e9, "dialect": "widget-line"})
        assert console.seen == mb.MAX_DEADLINE
    finally:
        broker.shutdown()


def test_a_secret_echoed_slowly_is_still_kept_out_of_the_log(tmp_path):
    # A long command is paced into the board and echoed back in pieces over more than a
    # second, so the log's held partial line is flushed between them: the scrub must not see
    # the secret in halves. Found with a certificate written through the settings shell.
    import io  # pylint: disable=import-outside-toplevel
    out = io.BytesIO()
    log = mb.ScrubbedLog(out)
    secret = "MIIBszCCAVmgAwIBAgIUQ" + "0" * 50
    log.hide(secret)
    log.write(b"settings write string tls/cert " + secret[:30].encode())
    log.flush(older_than=0.0)          # the broker's idle flush, mid-secret
    log.write(secret[30:].encode() + b"\n")
    log.flush()
    text = out.getvalue()
    assert secret.encode() not in text and b"<secret>" in text, text
    assert b"MIIBsz" not in text, text


def test_a_secret_the_shell_wrapped_at_the_terminal_width_is_kept_out_of_the_log():
    # The Zephyr shell echoes a long command wrapped at 80 columns: a carriage return and a
    # newline inside the secret, which a line-by-line scrub never matched (a certificate).
    import io  # pylint: disable=import-outside-toplevel
    out = io.BytesIO()
    log = mb.ScrubbedLog(out)
    secret = "MIIBszCCAVmgAwIBAgIUQ" + "0" * 50
    log.hide(secret)
    log.write(b"\x1b[1;32mzapp:~$ \x1b[msettings write string tls/cert " + secret[:39].encode() + b"\r\n"
              + secret[39:].encode() + b"\r\n")
    text = out.getvalue()
    assert secret.encode() not in text and b"MIIBsz" not in text, text
    assert b"<secret>" in text, text
    # the same, paced: the wrap arrives after an idle flush
    out = io.BytesIO()
    log = mb.ScrubbedLog(out)
    log.hide(secret)
    log.write(b"settings write string tls/cert " + secret[:39].encode() + b"\r\n" + secret[39:45].encode())
    log.flush(older_than=0.0)
    log.write(secret[45:].encode() + b"\r\n")
    text = out.getvalue()
    assert b"MIIBsz" not in text and b"<secret>" in text, text


def test_a_long_secret_against_a_shells_output_is_scrubbed_in_linear_time():
    # NSH's output is carriage returns, newlines and erase-line sequences everywhere; a
    # 72-byte certificate hidden in the log made the first pattern backtrack without end,
    # and the console thread with it (found as `cat /data/cert` timing out on a NuttX board).
    import io  # pylint: disable=import-outside-toplevel
    out = io.BytesIO()
    log = mb.ScrubbedLog(out)
    secret = "MIIBszCCAVmgAwIBAgIUQnb1cert0" + "0" * 43
    log.hide(secret)
    noise = (b"nsh> \x1b[Kcat /data/psk\r\r\nnsh: cat: open failed: 2\r\r\n" * 300)
    t0 = time.monotonic()
    log.write(noise + b"echo " + secret[:40].encode() + b"\r\r\n" + secret[40:].encode() + b" > /data/cert\r\r\n")
    log.flush()
    took = time.monotonic() - t0
    text = out.getvalue()
    assert took < 2.0, took
    assert b"MIIBsz" not in text and b"<secret>" in text, text[-300:]


class _Lines:
    """A serial port that records where its modem lines are put, in order."""

    def __init__(self):
        self.moves, self._dtr, self._rts = [], True, True

    dtr = property(lambda self: self._dtr, lambda self, v: (setattr(self, "_dtr", v), self.moves.append(("dtr", v))))
    rts = property(lambda self: self._rts, lambda self, v: (setattr(self, "_rts", v), self.moves.append(("rts", v))))


@pytest.mark.parametrize("rest, ends", [("asserted", (True, True)), ("deasserted", (False, False))])
def test_a_reset_pulses_en_and_leaves_the_lines_at_the_boards_rest_state(tmp_path, rest, ends):
    """DTR low with RTS high is the EN pulse on both an auto-reset circuit and a bridge whose RTS
    drives EN directly; where the lines go afterwards is the board's: a bridge of the second
    kind is held in reset for as long as RTS stays asserted (a CH340K handheld)."""
    console = mb.Console("m9", lambda: ("/dev/null", _Lines()), str(tmp_path / "m9.log"), modem_rest=rest)
    console.port, console.ser, *_ = console.opener()
    console.ser.moves.clear()
    console.reset()
    moves = console.ser.moves
    assert moves[:2] == [("dtr", False), ("rts", True)], "the EN pulse first"
    assert (console.ser.dtr, console.ser.rts) == ends
    if rest == "deasserted":
        assert moves.index(("rts", False)) < moves.index(("dtr", False), 1), "RTS released before DTR: RTS alone holds EN"


def test_the_rest_state_is_applied_at_the_open(monkeypatch, tmp_path):
    """A port opened with the lines where the board does not want them resets it at the open
    (both asserted on a bridge whose RTS holds EN) or straps it (both deasserted on a native
    USB-Serial/JTAG): the opener puts them where the board's rest state says, before the open."""
    import serial as pyserial

    class Recording(_Lines):
        def __init__(self):
            super().__init__()
            self.port = self.baudrate = self.timeout = self.write_timeout = None
            self.opened_with = None

        def open(self):
            self.opened_with = (self.dtr, self.rts)
    monkeypatch.setattr(pyserial, "Serial", Recording)
    _port, ser = mb.path_opener("/dev/ttyFAKE", modem_rest="deasserted")()
    assert ser.opened_with == (False, False)
    _port, ser = mb.path_opener("/dev/ttyFAKE")()
    assert ser.moves == [], "the default leaves a pty's lines alone (nothing to drive there)"


def test_the_modem_rest_comes_from_the_argument_or_the_environment(monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def opener(path, baud=115200, modem_rest="asserted"):
        seen["rest"] = modem_rest
        raise Stop()
    monkeypatch.setattr(mb, "path_opener", opener)
    monkeypatch.setenv("RXRBENCH_MODEM_REST", "deasserted")
    with pytest.raises(Stop):
        mb.main(["--board", "x", "--socket", "/nonexistent/x.sock", "--log", "/nonexistent/x.log", "--port", "/dev/null"])
    assert seen["rest"] == "deasserted", "the environment, as the unit's EnvironmentFile sets it"
    with pytest.raises(Stop):
        mb.main(["--board", "x", "--socket", "/nonexistent/x.sock", "--log", "/nonexistent/x.log", "--port", "/dev/null",
                 "--modem-rest", "asserted"])
    assert seen["rest"] == "asserted", "the argument wins"
    monkeypatch.setenv("RXRBENCH_MODEM_REST", "sideways")
    with pytest.raises(SystemExit):
        mb.main(["--board", "x", "--socket", "/nonexistent/x.sock", "--log", "/nonexistent/x.log", "--port", "/dev/null"])


def test_a_board_back_from_a_long_absence_answers_the_next_command_at_once(tmp_path):
    """A board minutes in its ROM bootloader (found on hardware: an RP2350 held in BOOTSEL)
    pushes the broker's next reopen up to 30 s out; it comes back seconds before the command,
    which must not fail on a port the broker would only have tried again later."""
    shell = FakeShell({"meshtastic version": VERSION})
    away = {"now": True}
    real = mb.path_opener(shell.path)

    def opener():
        if away["now"]:
            raise OSError("could not open port 2e8a:000f")   # what devreg resolved in BOOTSEL
        return real()

    console = mb.Console("sim1", opener, str(tmp_path / "sim1.log"))
    broker = mb.Broker(console, str(tmp_path / "run" / "sim1.sock"))
    t = threading.Thread(target=broker.run, daemon=True)
    t.start()
    try:
        time.sleep(1.0)
        assert not console.is_open()
        console.backoff, console.next_open = 30.0, time.monotonic() + 30.0
        away["now"] = False
        t0 = time.monotonic()
        with broker_client.Board("sim1", path=broker.sock_path, timeout=20) as b:
            out = b.exec("meshtastic version", deadline=8.0)
        assert "class:" in out and time.monotonic() - t0 < 5.0
    finally:
        broker.shutdown()
        t.join(timeout=2)
        shell.close()
