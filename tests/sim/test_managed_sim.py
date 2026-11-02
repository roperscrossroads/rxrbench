"""A managed node on the simulated bench: it is made managed last, it stays managed through a
reboot with what was written before, and afterwards the tool refuses to write to it rather
than write into the void. Needs the sim board built; skipped without it."""
import json
import os
import subprocess
import time

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.plugins.module_utils import state
from ansible_collections.rxrbench.mesh.sim import simboard
from tests.sim import test_sim_layout
from tests.sim.test_sim_layout import bench  # noqa: F401  (two boards of layout sim)

REPO, VENV = test_sim_layout.REPO, test_sim_layout.VENV
pytestmark = pytest.mark.skipif(not simboard.available(),
                                reason="sim board not built (just sim-build)")

ADMIN = "hex:8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"


def module(run, name, args, *flags):
    """One module against one simulated board, as a play would run it."""
    env = {**os.environ, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0",
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    # Run outside a layout's apply, so the go-ahead is given here (module_utils/gate.py).
    args = {"board": "sim2", "socket": os.path.join(run, "sim2.sock"), "apply_allowed": True, **args}
    r = subprocess.run([os.path.join(VENV, "ansible"), "sim2", "-i", "site.example/layouts/sim",
                        "-e", f"ansible_python_interpreter={os.path.join(VENV, 'python')}",
                        "-m", name, "-a", json.dumps(args), *flags],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False)
    return r.returncode, r.stdout + r.stderr


def facts(run):
    """The board's state line, waited for through a restart."""
    end, why = time.monotonic() + 30, None
    while time.monotonic() < end:
        try:
            with broker_client.Board("sim2", path=os.path.join(run, "sim2.sock")) as b:
                return state.read(b)
        except (broker_client.BrokerError, state.StateError) as e:
            why = e
            time.sleep(0.5)
    raise AssertionError(f"sim2 did not answer: {why}")


def test_a_node_is_made_managed_last_and_then_takes_nothing_from_the_bench(bench):  # noqa: F811
    want = {"owner": {"long_name": "managed2", "short_name": "mg2"},
            "security": {"admin_key": [ADMIN], "is_managed": True}}
    rc, out = module(bench, "rxrbench.mesh.meshtastic_config", want)
    assert rc == 0, out
    assert '"is_managed"' in out and '"owner": true' in out

    # What was written before the node became managed is kept through the restart that
    # follows (the module waits it out): written in one transaction with it, the commit is
    # dropped and all of it lost.
    assert '"restarted": true' in out, out
    after = facts(bench)
    assert after["managed"] is True
    assert after["owner"] == {"long_name": "managed2", "short_name": "mg2"}

    rc, out = module(bench, "rxrbench.mesh.meshtastic_config", want)
    assert rc == 0 and '"changed": false' in out, out

    # A plan that differs is refused, with what differs; nothing local unmanages it.
    for flags in (("--check",), ()):
        rc, out = module(bench, "rxrbench.mesh.meshtastic_config",
                         {"owner": {"long_name": "other"}, "security": {"is_managed": False}},
                         *flags)
        assert rc != 0 and "the node is managed" in out and "owner, security" in out, out
    assert facts(bench)["owner"]["long_name"] == "managed2"
    # its radio is no different: the shell refuses too, and the tool says why first
    rc, out = module(bench, "rxrbench.mesh.meshtastic_radio", {"lora": {"tx_power": 5}})
    assert rc != 0 and "the node is managed" in out and "tx_power" in out, out
