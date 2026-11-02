"""`just diag` end to end: the play, a real broker in front of a fake shell, and a chip behind a
fake debug probe. Each board is asked through what reaches it (its console, or its probe, read
without halting it) and its answers land in runs/<time>/<board>/; one that nothing reaches is
named and leaves nothing. Found on hardware: an SWD board was asked nothing."""
import glob
import importlib.util
import json
import os
import subprocess
import threading
import time

import pytest
import yaml

from tests.support.fake_shell import FakeShell

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAY = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "playbooks",
                    "diag.yml")
spec = importlib.util.spec_from_file_location("broker_for_diag", os.path.join(
    REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "broker.py"))
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)

UID = "003a001a3034511930363235"
# The bench host's tools as the module finds them under $HOME: devreg says where the probe is,
# swd_chip answers for the chip and notes how it was asked.
DEVREG = """import sys
print("1-1.2.4" if "--busid" in sys.argv else "0483:374b")
"""
SWD_CHIP = """import json, os, sys
a = sys.argv[1:]
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "asked"), "a").write(" ".join(a) + "\\n")
print(json.dumps({"uid": a[a.index("--uid") + 1], "idcode": "0x10016451",
                  "flash_size": 2097152, "voltage": 3.24}))
"""


@pytest.fixture
def bench(tmp_path):
    shell = FakeShell({"kernel version": "Zephyr version 4.5.0"}, ring=4096)
    console = mb.Console("c1", mb.path_opener(shell.path), str(tmp_path / "c1.log"))
    broker = mb.Broker(console, str(tmp_path / "run" / "c1.sock"))
    t = threading.Thread(target=broker.run, daemon=True)
    t.start()
    end = time.time() + 3
    while not console.is_open() and time.time() < end:
        time.sleep(0.01)
    home = tmp_path / "home"
    (home / "rxrbench" / "bin").mkdir(parents=True)
    (home / ".devreg").mkdir()
    (home / ".devreg" / "devreg.py").write_text(DEVREG)
    (home / "rxrbench" / "bin" / "swd_chip.py").write_text(SWD_CHIP)
    yield tmp_path, broker.sock_path, home
    broker.shutdown()
    t.join(timeout=2)
    shell.close()


def run(tmp_path, sock, home, s1_nrst=False):
    hosts = {
        "c1": {"bench_board": "c1", "bench_via": "console", "bench_socket": sock,
               "bench_family": "zephyr_settings",
               "bench_diag_commands": [{"name": "version", "cmd": "kernel version"}]},
        "s1": {"bench_board": "s1", "bench_via": None, "bench_family": "bare",
               "bench_flasher": "swd",
               "bench_routes": {"flash": {"probe": "stlink-s1", "chip": "STM32F767ZI",
                                          "uid": UID, "nrst": s1_nrst}}},
        "n1": {"bench_board": "n1", "bench_via": None, "bench_family": "bare"},
    }
    inv = {"boards": {"hosts": hosts,
                      "vars": {"ansible_connection": "local",
                               "ansible_python_interpreter": os.path.join(REPO, "venv", "bin", "python"),
                               "bench_layout": "t", "bench_repo": str(tmp_path),
                               "bench_runs_dir": str(tmp_path / "runs"),
                               "bench_families": {"zephyr_settings": {"console": "zephyr-shell"}}}}}
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump(inv))
    # The module runs its host tools with `python3`; under a fake HOME a version manager's shim
    # may refuse to run, so this test's python3 comes first.
    pybin = tmp_path / "pybin"
    pybin.mkdir(exist_ok=True)
    if not (pybin / "python3").exists():
        os.symlink(os.path.join(REPO, "venv", "bin", "python"), pybin / "python3")
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=os.path.join(REPO, "collections"),
               PATH=f"{pybin}:{os.environ['PATH']}",
               ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0", HOME=str(home),
               ANSIBLE_LOCAL_TEMP=str(tmp_path / "ansible-tmp"),
               ANSIBLE_REMOTE_TEMP=str(tmp_path / "ansible-tmp"))
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-playbook"), "-i",
                        str(tmp_path / "hosts.yml"), PLAY], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout + r.stderr


def record(tmp_path, board):
    hits = glob.glob(str(tmp_path / "runs" / "*" / board / "diag.json"))
    return json.load(open(hits[0])) if hits else None


def test_each_board_is_asked_through_what_reaches_it(bench):
    tmp, sock, home = bench
    rc, out = run(tmp, sock, home)
    assert rc == 0, out
    console = record(tmp, "c1")
    assert console and "Zephyr version 4.5.0" in console["results"][0]["output"], out
    swd = record(tmp, "s1")
    assert swd and swd["results"][0]["name"] == "swd-id", out
    assert swd["results"][0]["ok"], swd["results"][0]["output"]
    assert json.loads(swd["results"][0]["output"])["uid"] == UID
    asked = (home / "rxrbench" / "bin" / "asked").read_text()
    assert "id --look" in asked and "--location 1-1.2.4" in asked and f"--uid {UID}" in asked
    assert "reset" not in asked and "--srst" not in asked
    assert record(tmp, "n1") is None and "n1: no console, no ssh route and no debug probe" in out


def test_a_chip_reached_only_under_reset_is_named_and_not_touched(bench):
    tmp, sock, home = bench
    rc, out = run(tmp, sock, home, s1_nrst=True)
    assert rc == 0, out
    swd = record(tmp, "s1")
    assert swd["results"][0]["ok"] is False and "under reset" in swd["results"][0]["output"]
    assert not (home / "rxrbench" / "bin" / "asked").exists(), "the probe was never used"
