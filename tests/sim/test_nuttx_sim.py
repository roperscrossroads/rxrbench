"""NuttX on the PC bench (docs/SIMULATED.md: NSH on NuttX's sim board as a board of
the nuttx plugin's `nuttx_nsh` family, speaking on its standard streams on a pty the bench
owns, known by what it says to `uname -a`, its storage a host directory it mounts, its
configuration files written through the shell with the secret ones kept out of the log,
updated to another build and back. Needs the two NuttX images (`just sim-nuttx-build`) and
the rest of the layout's images; skipped without."""
import json
import os
import subprocess

import pytest
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client

from tests.sim import test_zephyr_sim
from tests.sim.test_zephyr_sim import bench, recap  # noqa: F401  (the six boards)

REPO, VENV = test_zephyr_sim.REPO, test_zephyr_sim.VENV
STORE = os.path.join(REPO, ".build", "store", "nuttx")
PB = "collections/ansible_collections/rxrbench/core/playbooks"

pytestmark = pytest.mark.skipif(
    not all(os.access(os.path.join(STORE, n, "nuttx"), os.X_OK) for n in ("nsh", "nsh-b")),
    reason="the NuttX images are not built (just sim-nuttx-build)")


def play(run, playbook, *args, timeout=600):
    env = {**os.environ, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0", "RXRBENCH_SITE": "site.example",
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", "site.example/layouts/zsim",
                        os.path.join(PB, playbook), "--limit", "nb1", *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout + r.stderr


def manifest(name):
    with open(os.path.join(STORE, name, "manifest.json")) as f:
        return json.load(f)


AS_B = ("-e", json.dumps({"firmware": {"family": "nuttx_nsh", "image": "nsh-b"}}))


def test_a_nuttx_board_is_known_by_what_it_says_and_keeps_its_files(bench):  # noqa: F811
    # The plan reads `uname -a` through the broker (the bench's pty), mounts the storage and
    # names the files that differ, by path only; the apply writes them; the next plan is clean.
    rc, out = play(bench, "apply.yml", "--check", "--diff")
    assert rc == 0 and recap(out)["nb1"]["failed"] == 0, out
    assert "nb1: mounted /data, /data/psk: set, /data/cert: set, /data/name: set" in out, out
    assert "psk-nb1-0123" not in out and "MIIBsz" not in out, out
    rc, out = play(bench, "apply.yml")
    assert rc == 0 and recap(out)["nb1"]["changed"] >= 1, out
    rc, out = play(bench, "apply.yml", "--check")
    assert rc == 0 and recap(out)["nb1"]["changed"] == 0, out
    # The files are there for the device tests and the facts; the secrets as declared.
    rc, out = play(bench, "test.yml")
    assert rc == 0 and "FAIL" not in out, out
    rc, out = play(bench, "facts.yml")
    assert rc == 0 and "nb1: /data/name=nb1" in out, out
    rc, out = play(bench, "secrets.yml")
    assert rc == 0 and "psk (declared): /data/psk, as declared" in out, out
    assert "cert (declared): /data/cert, as declared" in out, out
    # Nothing secret in the broker's log; the shell's echo of a write is redacted.
    with open(os.path.join(bench, "nb1.log"), "rb") as f:
        log = f.read()
    assert b"psk-nb1-0123" not in log and b"MIIBsz" not in log, log[-1500:]
    assert b"echo <secret> > /data/psk" in log, log[-1500:]
    # The host directory is the board's storage: the files are there on the host.
    with open(os.path.join(bench, "nb1.data", "name")) as f:
        assert f.read().strip() == "nb1"


def test_the_console_answers_every_read_in_a_long_run(bench):  # noqa: F811
    # Thirty identity reads back to back through the broker, each one `uname -a` typed and
    # read to a marker (19 bytes); none fails. NuttX's sim console in its default DMA mode
    # held every 256th byte typed until the byte after it (the ring's last slot, a blocking
    # read of the rest of the ring), so the fourteenth read of a board since its boot
    # failed with `no echo` or `timeout` (the nuttx plugin's tools/nuttx_sim.py sets the console's mode).
    with broker_client.Board("nb1", path=os.path.join(bench, "nb1.sock")) as board:
        for i in range(30):
            out = board.exec("uname -a", deadline=10.0)
            assert "NuttX " in out, (i, out)


def test_another_build_is_an_update_and_the_files_stay(bench):  # noqa: F811
    rc, out = play(bench, "apply.yml")
    assert rc == 0, out
    # The plan names the other build by what it says of itself; the apply restarts the board
    # on it, and its storage (a host directory) is where it was.
    rc, out = play(bench, "apply.yml", "--check", "--diff", *AS_B)
    assert rc == 0 and recap(out)["nb1"]["changed"] == 1, out
    assert manifest("nsh-b")["build"] in out and manifest("nsh")["build"] in out, out
    rc, out = play(bench, "apply.yml", *AS_B)
    assert rc == 0 and recap(out)["nb1"]["failed"] == 0, out
    rc, out = play(bench, "apply.yml", "--check", *AS_B)
    assert rc == 0 and recap(out)["nb1"]["changed"] == 0, out
    rc, out = play(bench, "test.yml", *AS_B)
    assert rc == 0 and "FAIL" not in out, out
    # And back.
    rc, out = play(bench, "apply.yml")
    assert rc == 0 and recap(out)["nb1"]["changed"] == 1, out
    rc, out = play(bench, "test.yml")
    assert rc == 0 and "FAIL" not in out, out
