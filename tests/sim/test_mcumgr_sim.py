"""MCUmgr on native_sim (docs/SIMULATED.md: Zephyr's SMP server beside the shell on a
simulated board's pty, a board of the `zephyr_mcumgr` family that the SMP flasher drives
through the port its broker releases. What a PC can prove: the board is known by the
executable it runs, an image is uploaded whole into the spare slot and marked for test, the
reset brings the board back through its broker, and the flasher says plainly that nothing
swapped the image in (native_sim has no bootloader: the swap is the QEMU board's).
Needs the four images (`just sim-zephyr-build`) and the MCUmgr venv the module runs with
(`~/.venv-mcumgr`, what `just hosts-install` makes); skipped without."""
import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.sim import test_zephyr_sim
from tests.sim.test_zephyr_sim import bench, recap  # noqa: F401  (the five boards)

REPO, VENV, STORE = test_zephyr_sim.REPO, test_zephyr_sim.VENV, test_zephyr_sim.STORE
PB = "collections/ansible_collections/rxrbench/core/playbooks"
MCUMGR_PY = os.path.expanduser("~/.venv-mcumgr/bin/python")
sys.path.insert(0, os.path.join(REPO, "collections"))
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client  # noqa: E402  pylint: disable=wrong-import-position

pytestmark = pytest.mark.skipif(
    not all(os.access(os.path.join(STORE, n, "zephyr.exe"), os.X_OK)
            for n in ("zephyr-shell", "zephyr-shell-b", "zapp-1.0", "zsmp", "zsmp-b"))
    or not os.access(MCUMGR_PY, os.X_OK),
    reason="needs the images (just sim-zephyr-build, just sim-zapp-build) and ~/.venv-mcumgr (just hosts-install)")


def play(run, playbook, *args, timeout=600):
    env = {**os.environ, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0", "RXRBENCH_SITE": "site.example",
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", "site.example/layouts/zsim",
                        os.path.join(PB, playbook), "--limit", "zb5", *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout + r.stderr


def manifest(name):
    with open(os.path.join(STORE, name, "manifest.json")) as f:
        return json.load(f)


AS_B = ("-e", json.dumps({"firmware": {"family": "zephyr_mcumgr", "image": "zsmp-b"}}))

SLOTS = '''
import asyncio, json, sys
from smpclient import SMPClient
from smpclient.transport.serial import SMPSerialTransport, BufferParams
from smpclient.requests.image_management import ImageStatesRead
async def main():
    c = SMPClient(SMPSerialTransport(BufferParams(line_length=128, line_buffers=10)), sys.argv[1], timeout_s=10.0)
    await c.connect()
    r = await c.request(ImageStatesRead())
    print(json.dumps([{"slot": i.slot, "hash": i.hash.hex() if i.hash else None, "active": bool(i.active),
                       "pending": bool(i.pending), "version": i.version} for i in r.images]))
    await c.disconnect()
asyncio.run(main())
'''


def slots(run):
    """The board's image slots as MCUmgr reports them, through the port its broker releases
    (the way the flasher reaches it)."""
    with broker_client.Board("zb5", path=os.path.join(run, "zb5.sock"), timeout=60) as b:
        port = b.release(ttl=60)
        try:
            r = subprocess.run([MCUMGR_PY, "-c", SLOTS, port], capture_output=True, text=True, timeout=60, check=False)
        finally:
            b.reacquire()
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_board_that_takes_images_over_mcumgr_is_known_by_what_it_runs(bench):  # noqa: F811
    # No bootloader put anything in a slot: the board is the executable its process is, and
    # the plan reads that through the SMP flasher (the board tells it so over SMP first).
    assert slots(bench) == [], "a fresh board has nothing in its slots"
    rc, out = play(bench, "apply.yml", "--check", "--diff")
    assert rc == 0 and recap(out)["zb5"]["failed"] == 0 and recap(out)["zb5"]["changed"] == 0, out
    rc, out = play(bench, "test.yml")
    assert rc == 0 and "FAIL" not in out, out


def test_an_upload_lands_in_the_spare_slot_and_the_flasher_says_nothing_swapped_it(bench):  # noqa: F811
    # The plan: the hash would change, nothing is written.
    rc, out = play(bench, "apply.yml", "--check", "--diff", *AS_B)
    assert rc == 0 and recap(out)["zb5"]["changed"] == 1, out
    assert manifest("zsmp-b")["hash"][:12] in out, out
    # The apply: the whole image goes up over SMP into the spare slot, is marked for test,
    # the board is reset and comes back through its broker; then the truth of a native_sim.
    rc, out = play(bench, "apply.yml", *AS_B, timeout=900)
    assert rc != 0, out
    assert "the spare slot holds the image, marked for test, but nothing swapped it in" in out, out
    assert "a simulated board has no bootloader" in out, out
    spare = [s for s in slots(bench) if not s["active"]]
    assert spare and spare[0]["hash"] == manifest("zsmp-b")["hash"] and spare[0]["pending"], spare
    assert spare[0]["version"].startswith("1.1.0"), spare
    # The board still runs what it ran, and says so to a plan; its console is back.
    rc, out = play(bench, "apply.yml", "--check")
    assert rc == 0 and recap(out)["zb5"]["changed"] == 0, out
    rc, out = play(bench, "test.yml")
    assert rc == 0 and "FAIL" not in out, out
    # An erase of the flash file empties the spare slot.
    rc, out = play(bench, "erase-chip.yml", "-e", "erase_without_backup=true")
    assert rc == 0 and "the flash file erased" in out, out
    assert slots(bench) == [], "after the erase the slots are empty again"


@pytest.mark.parametrize("client", ["mcumgr", "smpmgr"])
def test_other_mcumgr_clients_speak_to_the_same_board(bench, client):  # noqa: F811
    """The Go `mcumgr` and Intercreate's `smpmgr`, when installed, read the same slots through
    the same released port: the board is any client's."""
    # on PATH, else where it is usually installed: Go's bin for mcumgr, the MCUmgr venv for smpmgr
    usual = {"mcumgr": "~/go/bin/mcumgr", "smpmgr": "~/.venv-mcumgr/bin/smpmgr"}[client]
    exe = shutil.which(client) or os.path.expanduser(usual)
    if not exe or not os.access(exe, os.X_OK):
        pytest.skip(f"{client} is not installed")
    with broker_client.Board("zb5", path=os.path.join(bench, "zb5.sock"), timeout=60) as b:
        port = b.release(ttl=60)
        try:
            if client == "mcumgr":
                argv = [exe, "--conntype", "serial", "--connstring", f"dev={port},baud=115200,mtu=1024",
                        "-t", "10", "echo", "hello"]
            else:
                argv = [exe, "--port", port, "os", "echo", "hello"]
            r = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
        finally:
            b.reacquire()
    assert r.returncode == 0 and "hello" in r.stdout + r.stderr, r.stdout + r.stderr
