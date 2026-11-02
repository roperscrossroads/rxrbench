"""Flashing, on the simulated bench: the same four steps a real board gets (release the
console, write, reset, read back), with a process for a board. Needs both sim images
(`just sim-build main 0` and `just sim-build main 90`); skipped without them."""
import json
import os
import shutil

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.mesh.sim import simboard
from tests.sim.test_sim_layout import bench, facts_of, play, recap  # noqa: F401  (bench is a fixture)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORE = os.path.join(REPO, ".build", "store", "zephyrtastic")
OTHER = os.path.join(STORE, "sim-90")

pytestmark = pytest.mark.skipif(
    not (simboard.available() and os.path.exists(os.path.join(OTHER, "manifest.json"))),
    reason="sim images not built (just sim-build main 0; just sim-build main 90)")

AS_CLASS_90 = ("--limit", "sim1", "-e", json.dumps({"firmware": {"family": "zephyrtastic", "class": 90}}))


def broker_status(run, name):
    with broker_client.Board(name, path=os.path.join(run, f"{name}.sock")) as b:
        return b.status()


def test_a_declared_image_is_flashed_once_and_the_settings_survive(bench):  # noqa: F811
    rc, out = play(bench, "apply.yml")
    assert rc == 0, out
    assert recap(out)["sim1"]["changed"] == 2, "radio and config; the image already matches"
    assert facts_of(bench, "sim1")["class"] == 0
    opens = broker_status(bench, "sim1")["opens"]

    # The plan for another class says a flash is needed and why, and writes nothing.
    rc, out = play(bench, "apply.yml", "--check", "--diff", *AS_CLASS_90)
    assert rc == 0, out
    assert recap(out)["sim1"]["changed"] == 1, out
    assert '-    "class": 0' in out and '+    "class": 90' in out, out
    assert facts_of(bench, "sim1")["class"] == 0, "a plan must not flash"

    # The apply flashes it: a new image, a reboot, the same config.
    rc, out = play(bench, "apply.yml", *AS_CLASS_90)
    assert rc == 0, out
    assert recap(out)["sim1"] == {**recap(out)["sim1"], "changed": 1, "failed": 0}, out
    f = facts_of(bench, "sim1")
    assert f["class"] == 90
    assert f["owner"]["long_name"] == "sim1" and f["lora"]["modem_preset"] == "SHORT_FAST", \
        "a flash keeps the board's config"
    assert broker_status(bench, "sim1")["opens"] == opens + 1, "the console was given up once"

    # And again: nothing to do.
    rc, out = play(bench, "apply.yml", *AS_CLASS_90)
    assert rc == 0, out
    assert recap(out)["sim1"]["changed"] == 0, out
    assert facts_of(bench, "sim2")["class"] == 0, "the other board was not touched"


def test_an_image_for_another_board_is_refused_before_the_console_is_released(bench, tmp_path):  # noqa: F811
    wrong = tmp_path / "wrong"
    shutil.copytree(OTHER, wrong)
    manifest = json.loads((wrong / "manifest.json").read_text())
    manifest["board_tag"] = "heltec-v4"
    (wrong / "manifest.json").write_text(json.dumps(manifest))
    opens = broker_status(bench, "sim1")["opens"]

    rc, out = play(bench, "apply.yml", *AS_CLASS_90, "-e", f"bench_image={wrong}")
    assert rc != 0, out
    assert "refused: the image is for a 'heltec-v4' board; this board is a 'native-sim'" in out
    status = broker_status(bench, "sim1")
    assert status["opens"] == opens and status["open"], "the console never left the broker"
    assert facts_of(bench, "sim1")["class"] == 0, "and nothing was written"


def test_an_image_of_another_class_than_declared_is_refused(bench):  # noqa: F811
    rc, out = play(bench, "apply.yml", "--limit", "sim1", "-e", f"bench_image={OTHER}")
    assert rc != 0, out
    assert "the image is class 90; the layout declares class 0" in out
    assert facts_of(bench, "sim1")["class"] == 0
