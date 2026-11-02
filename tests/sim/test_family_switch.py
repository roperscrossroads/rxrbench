"""A board switched between firmware families and back, end to end: the sim board sim1 runs
zephyrtastic (layout `sim`), is applied as a zephcore repeater (layout `simswitch`), then as
zephyrtastic again. The flasher finds it running the other family each time (a family switch,
not a first flash) and the board's vocabulary tasks configure it. Needs `just sim-build` and
`just sim-build-zephcore`; skipped without them."""
import os
import signal
import subprocess
import sys
import time

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import meshcore, state
from ansible_collections.rxrbench.mesh.sim import simboard

ZEPHCORE = os.path.join(simboard.STORE, "zephcore", "sim-repeater", "zephyr.exe")
pytestmark = pytest.mark.skipif(not (simboard.available() and os.access(ZEPHCORE, os.X_OK)),
                                reason="needs just sim-build and just sim-build-zephcore")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(REPO, "venv", "bin")


@pytest.fixture
def bench(tmp_path):
    run = str(tmp_path / "run")
    out = open(tmp_path / "simbench.out", "wb")  # pylint: disable=consider-using-with
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(REPO, "collections/ansible_collections/rxrbench/mesh/sim", "simbench.py"),
                             "--run-dir", run, "sim1"], stdout=out, stderr=subprocess.STDOUT)
    end = time.monotonic() + 15
    while not os.path.exists(os.path.join(run, "simbench.pid")):
        assert proc.poll() is None, open(tmp_path / "simbench.out").read()
        assert time.monotonic() < end, "the simulated bench did not come up"
        time.sleep(0.05)
    yield run
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    out.close()


def play(run, layout, *args):
    env = {**os.environ, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0",
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i",
                        f"site.example/layouts/{layout}", os.path.join("collections/ansible_collections/rxrbench/core/playbooks", "apply.yml"),
                        "--limit", "sim1", *args], cwd=REPO, env=env, capture_output=True,
                       text=True, timeout=300, check=False)
    return r.returncode, r.stdout + r.stderr


def changed(out):
    for line in out.splitlines():
        if line.startswith("sim1") and "changed=" in line:
            return int(line.split("changed=")[1].split()[0])
    raise AssertionError(out)


def test_a_board_switches_family_and_back_keeping_each_family_s_settings(bench):
    sock = os.path.join(bench, "sim1.sock")
    rc, out = play(bench, "sim")
    assert rc == 0, out
    with broker_client.Board("sim1", path=sock) as b:
        before = state.state_facts(b.exec(state.STATE_COMMAND))

    rc, out = play(bench, "simswitch")
    assert rc == 0, out
    assert "family switch" in out or changed(out) > 0
    with broker_client.Board("sim1", path=sock) as b:
        f = meshcore.read(b, "zephcore")
    assert f["family"] == "zephcore" and f["name"] == "sim1zc" and f["radio"]["tx_dbm"] == 2
    assert f["pw_fp"] == meshcore.pw_fp("sim-pass-sw1-01")
    rc, out = play(bench, "simswitch", "--check")
    assert rc == 0 and changed(out) == 0, out

    rc, out = play(bench, "sim")
    assert rc == 0, out
    assert changed(out) == 1, "only the image: zephyrtastic's config was kept " + out
    with broker_client.Board("sim1", path=sock) as b:
        after = state.state_facts(b.exec(state.STATE_COMMAND))
    assert after["node_id"] == before["node_id"] and after["owner"] == before["owner"]
    rc, out = play(bench, "sim", "--check")
    assert rc == 0 and changed(out) == 0, out
