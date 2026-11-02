"""The MeshCore family end to end: `site.example/layouts/zcsim`, two zephcore repeaters (the real
firmware on native_sim) behind real brokers in the MeshCore console dialect, through Ansible.
Needs `just sim-build-zephcore`; skipped without it."""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import meshcore
from ansible_collections.rxrbench.mesh.sim import simboard

IMAGE = os.path.join(simboard.STORE, "zephcore", "sim-repeater", "zephyr.exe")
pytestmark = pytest.mark.skipif(not os.access(IMAGE, os.X_OK),
                                reason="zephcore sim not built (just sim-build-zephcore)")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(REPO, "venv", "bin")
BOARDS = ("zc1", "zc2")


@pytest.fixture
def bench(tmp_path):
    run = str(tmp_path / "run")
    out = open(tmp_path / "simbench.out", "wb")  # pylint: disable=consider-using-with
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(REPO, "collections/ansible_collections/rxrbench/mesh/sim", "simbench.py"),
                             "--run-dir", run, *BOARDS], stdout=out, stderr=subprocess.STDOUT)
    end = time.monotonic() + 15
    while not os.path.exists(os.path.join(run, "simbench.pid")):
        assert proc.poll() is None, open(tmp_path / "simbench.out").read()
        assert time.monotonic() < end, "the simulated bench did not come up"
        time.sleep(0.05)
    yield run
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    out.close()


def play(run, *args):
    env = {**os.environ, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0",
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", "site.example/layouts/zcsim",
                        os.path.join("collections/ansible_collections/rxrbench/core/playbooks", "apply.yml"), *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=300, check=False)
    return r.returncode, r.stdout + r.stderr


def recap(out):
    r = {}
    for line in out.splitlines():
        name, sep, rest = line.partition(":")
        if sep and "changed=" in rest and "unreachable=" in rest:
            r[name.strip()] = {k: int(v) for k, v in (f.split("=") for f in rest.split())}
    return r


def facts(run, name):
    with broker_client.Board(name, path=os.path.join(run, f"{name}.sock")) as b:
        return meshcore.read(b, "zephcore")


def test_a_fresh_repeater_is_wiped_configured_and_then_left_alone(bench):
    fresh = facts(bench, "zc1")
    assert fresh["radio"]["tx_enabled"] is False, "a fresh zephcore node is receive-only"
    assert fresh["storage"]["epoch"] == 0

    rc, out = play(bench, "--check")
    assert rc == 0, out
    assert all(r["changed"] == 3 and r["failed"] == 0 for r in recap(out).values()), out
    assert facts(bench, "zc1")["storage"]["epoch"] == 0, "a plan must not write"

    rc, out = play(bench)
    assert rc == 0, out
    keys = set()
    for name in BOARDS:
        f = facts(bench, name)
        assert f["name"] == name and f["storage"]["epoch"] == 1
        assert (f["radio"]["freq_mhz"], f["radio"]["bw_khz"], f["radio"]["sf"], f["radio"]["cr"],
                f["radio"]["tx_dbm"], f["radio"]["tx_enabled"]) == (910.525, 62.5, 7, 5, 2, True)
        assert f["pw_fp"] == meshcore.pw_fp(f"sim-pass-{name}-01")
        assert f"sim-pass-{name}-01" not in out
        assert f"sim-pass-{name}-01" not in open(os.path.join(bench, f"{name}.log"), "rb").read() \
            .decode(errors="replace")
        keys.add(f["pubkey_fp"])
    assert len(keys) == 2, "each sim board has its own seed, so its own identity"

    rc, out = play(bench)
    assert rc == 0, out
    assert recap(out) and all(r["changed"] == 0 for r in recap(out).values()), out


def test_a_bumped_epoch_wipes_once_keeps_the_key_and_reapplies(bench):
    rc, out = play(bench)
    assert rc == 0, out
    key = facts(bench, "zc1")["pubkey_fp"]
    rc, out = play(bench, "--limit", "zc1", "-e", json.dumps({"storage": {"zephcore": {
        "epoch": 2, "keep_identity": True}}}))
    assert rc == 0, out
    f = facts(bench, "zc1")
    assert f["storage"]["epoch"] == 2 and f["pubkey_fp"] == key
    assert f["radio"]["tx_enabled"] is True and f["name"] == "zc1", "config re-applied"


def test_a_lost_identity_comes_back_from_its_escrowed_key(bench):
    import base64
    with broker_client.Board("zc1", path=os.path.join(bench, "zc1.sock"), timeout=90) as b:
        key = meshcore.key_export(b)
        before = meshcore.read(b, "zephcore")
        # A sim board's `wipe new` makes the same key again (native_sim's entropy is seeded
        # the same each run), so another identity is given the way a reflash would leave one.
        # A well-formed Ed25519 private key (clamped). MeshCore still refuses some (`set prv.key`
        # answers Error: it reserves some public keys), so another is drawn when one is.
        for _ in range(10):
            other = bytearray(os.urandom(64))
            other[0] &= 248
            other[31] = (other[31] & 63) | 64
            try:
                fresh = meshcore.key_import(b, "zephcore", bytes(other))
                break
            except meshcore.MeshCoreError as e:
                assert "set prv.key" in str(e), e
        assert fresh["pubkey_fp"] != before["pubkey_fp"], "another key is another node"
        after = meshcore.key_import(b, "zephcore", key)
        assert after["pubkey_fp"] == before["pubkey_fp"] and meshcore.key_export(b) == key
    log = open(os.path.join(bench, "zc1.log"), "rb").read()
    assert base64.b64encode(key)[:24] not in log and key.hex()[:32].encode() not in log
