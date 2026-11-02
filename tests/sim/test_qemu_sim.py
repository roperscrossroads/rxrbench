"""MCUboot on an emulated MCU (docs/SIMULATED.md: Arm's MPS2 AN385 under QEMU, MCUboot
in RAM-load mode with revert, Zephyr's SMP server as the application, the slots in a simulated
flash the machine keeps across a CPU reset. The board is one of core's sim registry (`zq1`,
console `qemu`), behind a real broker, driven by the SMP flasher through the port the broker
releases. What this proves that native_sim cannot: the bootloader's choice. An upload of a
higher version is marked for test, the reset makes MCUboot validate and run it, the flasher
confirms it; a lower version uploaded beside it is never booted (the mode runs the highest
version it holds) and the flasher says so; an image that reboots unconfirmed is erased at
the next boot (the revert); a power cycle is the factory image again (its flash is the
machine's RAM). Needs the two images (`just sim-qemu-build`: the Zephyr SDK's Arm toolchain),
`qemu-system-arm` and the MCUmgr venv (`~/.venv-mcumgr`); skipped without."""
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from tests.sim import test_zephyr_sim
from tests.sim.test_zephyr_sim import recap  # noqa: F401

REPO, VENV, STORE, SIM = test_zephyr_sim.REPO, test_zephyr_sim.VENV, test_zephyr_sim.STORE, test_zephyr_sim.SIM
REGISTRY = test_zephyr_sim.REGISTRY
PB = "collections/ansible_collections/rxrbench/core/playbooks"
MCUMGR_PY = os.path.expanduser("~/.venv-mcumgr/bin/python")
BOARD = "zq1"
sys.path.insert(0, os.path.join(REPO, "collections"))
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client  # noqa: E402  pylint: disable=wrong-import-position

pytestmark = pytest.mark.skipif(
    not all(os.path.isfile(os.path.join(STORE, n, "manifest.json")) for n in ("zq", "zq-b"))
    or not shutil.which("qemu-system-arm") or not os.access(MCUMGR_PY, os.X_OK),
    reason="needs the emulated MCU's images (just sim-qemu-build), qemu-system-arm and ~/.venv-mcumgr (just hosts-install)")



def _built_on():
    """The Zephyr release the emulated MCU's images were built from (the manifest's `zephyr`),
    as a tuple, or None when the manifest does not say (an image staged before it recorded it)."""
    try:
        with open(os.path.join(STORE, "zq", "manifest.json")) as f:
            v = json.load(f).get("zephyr")
        return tuple(int(x) for x in v.split(".")) if v else None
    except (OSError, ValueError):
        return None


# Before Zephyr 4.5, MCUmgr's image manager does not ask MCUboot which slot runs in
# RAM-load-with-revert mode and always reports slot 0, so an update that boots is read back as
# the old image (fixed upstream: "mcumgr: image management: ram load with revert", in 4.5).
pytestmark = [pytestmark, pytest.mark.skipif(
    (_built_on() or (4, 5, 0)) < (4, 5, 0),
    reason="the emulated MCU needs Zephyr 4.5 or newer: before it, MCUmgr's image manager reports "
           "slot 0 as running in RAM-load-with-revert mode whatever MCUboot loaded")]


@pytest.fixture
def bench(tmp_path):
    """The sim bench with the emulated board alone (`just sim-zephyr-up zq1`)."""
    run = str(tmp_path / "run")
    out = open(tmp_path / "simbench.out", "wb")  # pylint: disable=consider-using-with
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(SIM, "simbench.py"),
                             "--registry", REGISTRY, "--run-dir", run, BOARD],
                            stdout=out, stderr=subprocess.STDOUT)
    end = time.monotonic() + 15
    while not os.path.exists(os.path.join(run, "simbench.pid")):
        assert proc.poll() is None, open(tmp_path / "simbench.out").read()
        assert time.monotonic() < end, "the simulated bench did not come up"
        time.sleep(0.05)
    # Both images staged in the host store (a plan does that, as a change, the first time),
    # so the plans below count the board's changes alone.
    for extra in ((), AS_B):
        rc, text = play(run, "apply.yml", "--check", *extra)
        assert rc == 0, text
    yield run
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    out.close()


def play(run, playbook, *args, timeout=600):
    env = {**os.environ, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0", "RXRBENCH_SITE": "site.example",
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", "site.example/layouts/zsim",
                        os.path.join(PB, playbook), "--limit", BOARD, *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout + r.stderr


def manifest(name):
    with open(os.path.join(STORE, name, "manifest.json")) as f:
        return json.load(f)


AS_B = ("-e", json.dumps({"firmware": {"family": "zephyr_mcumgr", "image": "zq-b"}}))

# One MCUmgr session on the released port (the way the flasher reaches the board): the slots
# as the board reports them; or an upload marked for test and a reset, with no confirmation,
# or a bare reset (what a device does on its own, for MCUboot's revert to show).
SMP = '''
import asyncio, json, sys
from smpclient import SMPClient
from smpclient.transport.serial import SMPSerialTransport, BufferParams
from smpclient.requests.image_management import ImageStatesRead, ImageStatesWrite
from smpclient.requests.os_management import ResetWrite
async def main():
    c = SMPClient(SMPSerialTransport(BufferParams(line_length=128, line_buffers=2)), sys.argv[1], timeout_s=10.0)
    await c.connect()
    if sys.argv[2] == "upload-test-reset":
        with open(sys.argv[3], "rb") as f:
            data = f.read()
        async for _ in c.upload(data, first_timeout_s=60.0):
            pass
        await c.request(ImageStatesWrite(hash=bytes.fromhex(sys.argv[4]), confirm=False))
        await c.request(ResetWrite())
    elif sys.argv[2] == "reset":
        await c.request(ResetWrite())
    else:
        r = await c.request(ImageStatesRead())
        print(json.dumps([{"slot": i.slot, "hash": i.hash.hex() if i.hash else None, "active": bool(i.active),
                           "confirmed": bool(i.confirmed), "pending": bool(i.pending), "version": i.version}
                          for i in r.images]))
    await c.disconnect()
asyncio.run(main())
'''


def smp(run, *args, timeout=600):
    with broker_client.Board(BOARD, path=os.path.join(run, f"{BOARD}.sock"), timeout=60) as b:
        port = b.release(ttl=timeout)
        try:
            r = subprocess.run([MCUMGR_PY, "-c", SMP, port, *args], capture_output=True, text=True,
                               timeout=timeout, check=False)
        finally:
            b.reacquire()
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def slots(run):
    return json.loads(smp(run, "slots").strip().splitlines()[-1])


def active(run):
    return next(s for s in slots(run) if s["active"])


def test_mcuboot_boots_the_factory_image_and_the_board_is_known_by_it(bench):
    # MCUboot found the image QEMU placed in slot 0, validated it, copied it to RAM and ran
    # it; the image reports itself confirmed (the factory image carries a confirmed trailer,
    # as a programmer would leave it) and at its version. The plan reads the same over SMP.
    a = active(bench)
    assert a["hash"] == manifest("zq")["hash"] and a["confirmed"] and a["version"].startswith("1.0.0"), a
    rc, out = play(bench, "apply.yml", "--check", "--diff")
    assert rc == 0 and recap(out)[BOARD]["failed"] == 0 and recap(out)[BOARD]["changed"] == 0, out
    rc, out = play(bench, "test.yml")
    assert rc == 0 and "FAIL" not in out, out


def test_an_upgrade_is_the_bootloaders_choice_and_a_downgrade_its_refusal(bench):
    # The plan names the change by hash; the apply uploads the higher version into the spare
    # slot, marks it for test, resets: MCUboot runs it, the flasher confirms it.
    rc, out = play(bench, "apply.yml", "--check", "--diff", *AS_B)
    assert rc == 0 and recap(out)[BOARD]["changed"] == 1 and manifest("zq-b")["hash"][:12] in out, out
    rc, out = play(bench, "apply.yml", *AS_B, timeout=900)
    assert rc == 0 and recap(out)[BOARD]["failed"] == 0, out
    a = active(bench)
    assert a["hash"] == manifest("zq-b")["hash"] and a["confirmed"] and a["version"].startswith("2.0.0"), a
    rc, out = play(bench, "apply.yml", "--check", *AS_B)
    assert rc == 0 and recap(out)[BOARD]["changed"] == 0, out
    rc, out = play(bench, "test.yml", *AS_B)
    assert rc == 0 and "FAIL" not in out, out
    # Back to the lower version: uploaded and marked, but MCUboot runs the highest version it
    # holds, so the board comes back on the same image and the flasher says why. (On this
    # board the spare slot is the factory image again after the reset in any case: QEMU
    # rewrites what its loader placed at every CPU reset, so slot 0 cannot carry an upload
    # across one; the upgrade path is slot 1.)
    rc, out = play(bench, "apply.yml", timeout=900)
    assert rc != 0, out
    assert "the bootloader runs the highest version it holds and never a lower one" in out, out
    assert active(bench)["hash"] == manifest("zq-b")["hash"]
    spare = [s for s in slots(bench) if not s["active"]]
    assert spare and spare[0]["hash"] == manifest("zq")["hash"], spare
    # A power cycle: the flash was the machine's RAM, the board starts on its factory image.
    rc, out = play(bench, "erase-chip.yml", "-e", "erase_without_backup=true")
    assert rc == 0 and "the board power-cycled" in out, out
    a = active(bench)
    assert a["hash"] == manifest("zq")["hash"] and a["version"].startswith("1.0.0"), a
    rc, out = play(bench, "apply.yml", "--check")
    assert rc == 0 and recap(out)[BOARD]["changed"] == 0, out


def test_an_image_that_reboots_unconfirmed_is_reverted(bench):
    # What a device does on its own, without the flasher's confirmation: the upload runs
    # after the reset, on trial; the next reset is MCUboot's revert, which erases it.
    smp(bench, "upload-test-reset", os.path.join(STORE, "zq-b", "app.signed.bin"), manifest("zq-b")["hash"])
    time.sleep(3)
    a = active(bench)
    assert a["hash"] == manifest("zq-b")["hash"] and not a["confirmed"], a
    smp(bench, "reset")
    time.sleep(3)
    a = active(bench)
    assert a["hash"] == manifest("zq")["hash"], a
    assert not [s for s in slots(bench) if s["hash"] == manifest("zq-b")["hash"]], "the trial image is erased"
    # And a plan sees a board on its declared image, nothing to do.
    rc, out = play(bench, "apply.yml", "--check")
    assert rc == 0 and recap(out)[BOARD]["changed"] == 0, out
