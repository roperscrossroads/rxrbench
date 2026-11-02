"""The simulated bench as the tool sees it: `site.example/layouts/sim`, through Ansible, into real brokers
in front of real firmware. Needs the sim board built; skipped without it."""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from ansible_collections.rxrbench.mesh.plugins.module_utils import state
from ansible_collections.rxrbench.mesh.sim import simboard

pytestmark = pytest.mark.skipif(not simboard.available(),
                                reason="sim board not built (just sim-build)")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(REPO, "venv", "bin")


@pytest.fixture
def bench(tmp_path):
    """`just sim-up`, in a directory of its own."""
    run = str(tmp_path / "run")
    out = open(tmp_path / "simbench.out", "wb")  # pylint: disable=consider-using-with
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(REPO, "collections/ansible_collections/rxrbench/mesh/sim", "simbench.py"),
                             "--run-dir", run, "sim1", "sim2"],
                            stdout=out, stderr=subprocess.STDOUT)
    end = time.monotonic() + 15
    while not os.path.exists(os.path.join(run, "simbench.pid")):
        assert proc.poll() is None, open(tmp_path / "simbench.out").read()
        assert time.monotonic() < end, "the simulated bench did not come up"
        time.sleep(0.05)
    yield run
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    out.close()


def play(run, playbook, *args):
    env = {**os.environ, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0",
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", "site.example/layouts/sim",
                        os.path.join("collections/ansible_collections/rxrbench/core/playbooks", playbook), *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
                       check=False)
    return r.returncode, r.stdout + r.stderr


def test_facts_reads_both_simulated_boards(bench):
    rc, out = play(bench, "facts.yml")
    assert rc == 0, out
    # A fresh board is what the firmware's defaults make it, not yet what the layout declares.
    for name in ("sim1", "sim2"):
        assert f"{name}: image" in out, out
    assert out.count("class 0 | LONG_FAST 30 dBm tx on") == 2, out
    assert "failed=0" in out and "unreachable=0" in out


def test_the_two_boards_are_two_nodes(bench):
    from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
    from ansible_collections.rxrbench.mesh.plugins.module_utils import state
    ids = []
    for name in ("sim1", "sim2"):
        with broker_client.Board(name, path=os.path.join(bench, f"{name}.sock")) as b:
            ids.append(state.state_facts(b.exec(state.STATE_COMMAND))["node_id"])
    assert len(set(ids)) == 2, ids


def recap(out):
    """{board: {"ok": n, "changed": n, "failed": n, ...}} from a play recap."""
    r = {}
    for line in out.splitlines():
        name, sep, rest = line.partition(":")
        if sep and "changed=" in rest and "unreachable=" in rest:
            r[name.strip()] = {k: int(v) for k, v in (f.split("=") for f in rest.split())}
    return r


def facts_of(bench, name):
    from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
    from ansible_collections.rxrbench.mesh.plugins.module_utils import state
    with broker_client.Board(name, path=os.path.join(bench, f"{name}.sock")) as b:
        return state.state_facts(b.exec(state.STATE_COMMAND))


def lora_of(bench, name):
    return facts_of(bench, name)["lora"]


def test_plan_then_apply_then_apply_again_changes_nothing(bench):
    # The plan says what would change, and changes nothing.
    rc, out = play(bench, "apply.yml", "--check", "--diff")
    assert rc == 0, out
    # two steps would change each board: its radio config and its other config
    assert all(r["changed"] == 2 and r["failed"] == 0 for r in recap(out).values()), out
    assert '-    "modem_preset": "LONG_FAST"' in out, out
    assert '+    "modem_preset": "SHORT_FAST"' in out and '+    "tx_power": 2' in out, out
    assert '+        "long_name": "sim1"' in out and '+        "rebroadcast_mode": "ALL"' in out
    assert '+            "psk": "secret:' in out, "a key is planned as its fingerprint"
    assert "000102030405060708090a0b0c0d0e0f" not in out, "and never shown"
    before = facts_of(bench, "sim1")
    assert before["lora"]["modem_preset"] == "LONG_FAST", "a plan must not write"
    assert before["owner"]["long_name"] != "sim1" and len(before["channels"]) == 1

    # The apply makes it so.
    rc, out = play(bench, "apply.yml", "--diff")
    assert rc == 0, out
    assert all(r["changed"] == 2 and r["failed"] == 0 for r in recap(out).values()), out
    assert "000102030405060708090a0b0c0d0e0f" not in out
    for name in ("sim1", "sim2"):
        f = facts_of(bench, name)
        lora = f["lora"]
        assert (lora["modem_preset"], lora["live_preset"], lora["tx_power"],
                lora["tx_enabled"]) == ("SHORT_FAST", "SHORT_FAST", 2, True)
        # written by the stock library through the tunnel
        assert f["owner"] == {"long_name": name, "short_name": name}
        assert f["device"] == {"role": "CLIENT", "rebroadcast_mode": "ALL"}
        primary, side = f["channels"]
        assert (primary["name"], primary["role"], primary["psk"]) == ("ShortFast", "PRIMARY",
                                                                      "default")
        assert (side["index"], side["name"], side["role"], side["psk"]) == (1, "side",
                                                                            "SECONDARY", "secret")
        assert side["fingerprint"] == state.key_fingerprint(bytes(range(16)))

    # A second apply finds nothing to do, and so does the plan.
    for extra in ((), ("--check",)):
        rc, out = play(bench, "apply.yml", *extra)
        assert rc == 0, out
        assert recap(out) and all(r["changed"] == 0 and r["failed"] == 0
                                  for r in recap(out).values()), out


def test_a_radio_block_given_on_the_command_line_is_checked_where_it_is_written(bench):
    # The layout's own check reads the layout's files; `-e` arrives after it. The module that
    # writes the radio holds the block and the rules, and refuses before it writes.
    loud = {"meshtastic": {"config": {"lora": {"modem_preset": "SHORT_FAST", "tx_power": 20,
                                               "tx_enabled": True}}}}
    rc, out = play(bench, "apply.yml", "--limit", "sim1", "-e", json.dumps(loud))
    assert rc != 0, out
    assert "the RF rules refuse this write" in out and "20 dBm is above the ceiling of 2 dBm" in out, out
    assert lora_of(bench, "sim1")["tx_power"] == 30, "nothing was written"


def test_diagnostics_are_pulled_into_a_directory_per_device(bench, tmp_path):
    runs = tmp_path / "runs"
    rc, out = play(bench, "diag.yml", "-e", f"bench_runs_dir={runs}")
    assert rc == 0, out
    (run,) = list(runs.iterdir())
    for name in ("sim1", "sim2"):
        got = sorted(p.name for p in (run / name / "diag").iterdir())
        assert got == ["console.txt", "lora.txt", "resets.txt", "state.txt", "status.txt",
                       "version.txt"]
        assert "~S{" in (run / name / "diag" / "state.txt").read_text()
        assert "meshtastic version" in (run / name / "diag" / "console.txt").read_text(), \
            "the console log is the board's own, typed commands and all"
        record = json.loads((run / name / "diag.json").read_text())
        assert record["device"] == name and record["kind"] == "diag"
        assert [r["name"] for r in record["results"]][-1] == "console"
    # pulling diagnostics in a plan is allowed: it only reads
    rc, out = play(bench, "diag.yml", "--check", "-e", f"bench_runs_dir={runs}")
    assert rc == 0 and len(list(runs.iterdir())) >= 1, out


def test_device_tests_pass_fail_and_are_recorded(bench, tmp_path):
    runs = tmp_path / "runs"
    rc, out = play(bench, "test.yml", "-e", f"bench_runs_dir={runs}")
    assert rc == 0, out
    (run,) = list(runs.iterdir())
    record = json.loads((run / "sim1" / "test.json").read_text())
    assert [(r["name"], r["passed"]) for r in record["results"]] == [
        ("version", True), ("state", True), ("resets", True)]

    # A test that cannot pass fails the run, names the test, and is still recorded.
    bad = json.dumps({"bench_tests": [
        {"name": "version", "cmd": "meshtastic version", "expect": "^class:\\s+0\\b"},
        {"name": "wrong-class", "cmd": "meshtastic version", "expect": "^class:\\s+77\\b"}]})
    runs2 = tmp_path / "runs2"
    rc, out = play(bench, "test.yml", "--limit", "sim1", "-e", f"bench_runs_dir={runs2}", "-e", bad)
    assert rc != 0 and "sim1: failed wrong-class" in out, out
    (run,) = list(runs2.iterdir())
    record = json.loads((run / "sim1" / "test.json").read_text())
    failed = [r for r in record["results"] if not r["passed"]]
    assert [r["name"] for r in failed] == ["wrong-class"]
    assert failed[0]["why"] == ["output does not match /^class:\\s+77\\b/"]
