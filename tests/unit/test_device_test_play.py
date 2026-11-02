"""`just device-test` end to end: the play, a real broker, a fake shell behind it. Found on
hardware: a layout that declares no bench_tests is refused rather than reported as every board
passing."""
import importlib.util
import os
import subprocess
import threading
import time

import pytest
import yaml

from tests.support.fake_shell import FakeShell

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAY = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "playbooks",
                    "test.yml")
spec = importlib.util.spec_from_file_location("broker_for_device_tests", os.path.join(
    REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "broker.py"))
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)


@pytest.fixture
def bench(tmp_path):
    shell = FakeShell({"meshtastic version": "class: 4\nbuild: 1117570e"}, ring=4096)
    console = mb.Console("n1", mb.path_opener(shell.path), str(tmp_path / "n1.log"))
    broker = mb.Broker(console, str(tmp_path / "run" / "n1.sock"))
    t = threading.Thread(target=broker.run, daemon=True)
    t.start()
    end = time.time() + 3
    while not console.is_open() and time.time() < end:
        time.sleep(0.01)
    yield tmp_path, broker.sock_path
    broker.shutdown()
    t.join(timeout=2)
    shell.close()


def run(tmp_path, sock, n1_tests, n2_tests=None):
    def host(name, tests):
        return {"bench_board": name, "bench_via": "console", "bench_socket": sock,
                "bench_family": "zephyrtastic",
                **({"bench_tests": tests} if tests is not None else {})}
    inv = {"boards": {"hosts": {"n1": host("n1", n1_tests), "n2": host("n2", n2_tests)},
                      "vars": {"ansible_connection": "local",
                               "ansible_python_interpreter": os.path.join(REPO, "venv", "bin", "python"),
                               "bench_layout": "t", "bench_repo": str(tmp_path),
                               "bench_runs_dir": str(tmp_path / "runs")}}}
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump(inv))
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=os.path.join(REPO, "collections"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0")
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-playbook"), "-i",
                        str(tmp_path / "hosts.yml"), PLAY], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout + r.stderr


def test_a_run_where_nothing_declares_tests_is_refused_not_passed(bench):
    tmp, sock = bench
    rc, out = run(tmp, sock, None)
    assert rc != 0 and "no device in this run declares bench_tests" in out, out


def test_a_device_with_tests_is_judged_and_one_without_is_named(bench):
    tmp, sock = bench
    rc, out = run(tmp, sock, [{"name": "build", "cmd": "meshtastic version", "expect": "^build:\\s+1117570e"}])
    assert rc == 0, out
    assert "n2: declares no bench_tests, so nothing was tested on it" in out, out
    rc, out = run(tmp, sock, [{"name": "build", "cmd": "meshtastic version", "expect": "^build:\\s+deadbeef"}])
    assert rc != 0 and "n1: failed build" in out, out


def test_an_empty_list_of_tests_is_no_tests(bench):
    """Found on hardware: a layout whose tests depend on the firmware (none for a stock node with
    no console) gave an empty list, and the run reported every test passed."""
    tmp, sock = bench
    rc, out = run(tmp, sock, [])
    assert rc != 0 and "no device in this run declares bench_tests" in out, out
    rc, out = run(tmp, sock, [{"name": "build", "cmd": "meshtastic version",
                               "expect": "^build:\\s+1117570e"}], [])
    assert rc == 0, out
    assert "n2: declares no bench_tests, so nothing was tested on it" in out, out
