"""Stock Meshtastic with no hardware (layout `simmt`): meshtasticd, Meshtastic's own Linux build
with its simulated radio, in a container (mesh/sim/stock/simmt.py), configured through its
network API exactly as a stock node on the network is. Needs Docker and the pinned image;
skipped without them. Each test starts from a wiped node (a factory reset)."""
import os
import re
import subprocess
import sys

import pytest

from ansible_collections.rxrbench.mesh.sim.stock import simmt

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(REPO, "venv", "bin")


def image_present():
    if not simmt.available():
        return False
    return subprocess.run(["docker", "image", "inspect", simmt.IMAGE], capture_output=True,
                          check=False).returncode == 0


pytestmark = pytest.mark.skipif(not image_present(),
                                reason=f"no Docker or no {simmt.IMAGE} (docker pull it)")


def play(playbook, *args):
    env = {**os.environ, "ANSIBLE_FORCE_COLOR": "0", "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", "site.example/layouts/simmt",
                        os.path.join("collections/ansible_collections/rxrbench/core/playbooks", playbook),
                        *args], cwd=REPO, env=env, capture_output=True, text=True, timeout=600,
                       check=False)
    return r.returncode, r.stdout + r.stderr


def recap(out):
    m = re.search(r"mt1\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", out)
    assert m, out
    return dict(zip(("ok", "changed", "unreachable", "failed"), map(int, m.groups())))


@pytest.fixture
def node():
    tool = os.path.join(os.path.dirname(simmt.__file__), "simmt.py")
    subprocess.run([sys.executable, tool, "wipe", "mt1"], check=True, capture_output=True,
                   timeout=120)
    yield "mt1"
    # Nothing left behind: the container restarts with the machine (--restart unless-stopped).
    subprocess.run([sys.executable, tool, "down", "mt1", "--erase"], check=True,
                   capture_output=True, timeout=120)


def test_a_factory_node_is_planned_applied_and_then_left_alone(node):  # noqa: ARG001
    # The plan: what the layout declares against the firmware's defaults; nothing written.
    rc, out = play("apply.yml", "--check", "--diff")
    assert rc == 0, out
    assert recap(out)["changed"] == 1
    assert '+        "region": "US"' in out and '-        "region": "UNSET"' in out, out
    assert '+        "long_name": "bench mt1"' in out, out
    rc, out = play("apply.yml", "--check")
    assert recap(out)["changed"] == 1, "a plan must not write"

    # The apply: the radio (a reboot), then owner, hop limit and both channels in one edit
    # transaction (another reboot), then the admin key by itself. Every step read back.
    rc, out = play("apply.yml")
    assert rc == 0, out
    assert recap(out) == {**recap(out), "changed": 1, "failed": 0}, out

    # And again: the node shows everything declared, across its reboots.
    rc, out = play("apply.yml")
    assert rc == 0, out
    assert recap(out)["changed"] == 0, out

    rc, out = play("facts.yml")
    assert rc == 0, out
    assert "US SHORT_FAST 2 dBm tx on" in out and "bench mt1" in out, out
