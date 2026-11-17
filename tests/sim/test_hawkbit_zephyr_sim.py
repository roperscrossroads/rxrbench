"""The hawkBit route on a simulated Zephyr device (docs/SIMULATED.md): the emulated MCU of
test_qemu_sim.py, here running zapp with Zephyr's hawkBit client and a network, behind a real
broker, and the plugin's stand-in server on this machine. Nothing is uploaded from the bench:
the server is told which image the device should have, the device's own client fetches it
over the emulated network, reboots, MCUboot runs it and the device confirms itself. The bench
reads it over MCUmgr on its console before and after, as it does a board it writes.

It starts from nothing: the server has never heard of the device and the device's client has
no settings. Both are the layout's (`zsimhawkbit`), brought by the first apply.

Needs the two images (`just sim-zapp-qemu-build`: the Zephyr SDK's Arm toolchain),
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
from tests.sim.test_qemu_sim import MCUMGR_PY

REPO, VENV, STORE, SIM = test_zephyr_sim.REPO, test_zephyr_sim.VENV, test_zephyr_sim.STORE, test_zephyr_sim.SIM
HAWKBIT = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "hawkbit")
REGISTRY = os.path.join(HAWKBIT, "sim", "registry.yaml")
TOOL = os.path.join(HAWKBIT, "host", "bin", "hawkbit_route.py")
PB = "collections/ansible_collections/rxrbench/core/playbooks"
LAYOUT, BOARD = "zsimhawkbit", "zh1"
sys.path.insert(0, os.path.join(HAWKBIT, "sim"))
import fakeserver  # noqa: E402  pylint: disable=wrong-import-position,import-error

pytestmark = pytest.mark.skipif(
    not all(os.path.isfile(os.path.join(STORE, n, "manifest.json")) for n in ("zqh", "zqh-b"))
    or not shutil.which("qemu-system-arm") or not os.access(MCUMGR_PY, os.X_OK),
    reason="needs the simulated device's images (just sim-zapp-qemu-build), qemu-system-arm and "
           "~/.venv-mcumgr (just hosts-install)")


def manifest(name):
    with open(os.path.join(STORE, name, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def bench(tmp_path):
    """The stand-in server, turning away a device that shows another token than its target's
    and telling devices to ask every five seconds, and the emulated device alone on a bench."""
    run = str(tmp_path / "run")
    out = open(tmp_path / "simbench.out", "wb")  # pylint: disable=consider-using-with
    with fakeserver.FakeHawkbit(token_auth=True, polling="00:00:05") as server:
        proc = subprocess.Popen([sys.executable, "-u", os.path.join(SIM, "simbench.py"),
                                 "--registry", REGISTRY, "--run-dir", run, BOARD],
                                stdout=out, stderr=subprocess.STDOUT)
        try:
            end = time.monotonic() + 15
            while not os.path.exists(os.path.join(run, "simbench.pid")):
                assert proc.poll() is None, open(tmp_path / "simbench.out", encoding="utf-8").read()
                assert time.monotonic() < end, "the simulated bench did not come up"
                time.sleep(0.05)
            yield run, server
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
            out.close()


def play(run, server, *args, timeout=900):
    env = {**os.environ, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0", "RXRBENCH_SITE": "site.example",
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    # The server of this run in place of the layout's example: where this machine finds it,
    # and the port the device finds it on (its host is the emulated network's gateway).
    extra = {"bench_hawkbit": {"url": server.url, "user": "bench", "password": "made-up"},
             "bench_hawkbit_device_port": str(server.port)}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", f"site.example/layouts/{LAYOUT}",
                        os.path.join(PB, "apply.yml"), "--limit", BOARD, "-e", json.dumps(extra), *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout + r.stderr


def route(server, **request):
    r = subprocess.run([sys.executable, TOOL, "state"], capture_output=True, text=True, timeout=60, check=False,
                       input=json.dumps({"settings": {"url": server.url, "user": "bench", "password": "made-up"},
                                         "target": BOARD, **request}))
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_zephyr_device_fetches_its_image_from_the_server_and_is_read_over_mcumgr(bench):
    run, server = bench
    token = "0123456789abcdef0123456789abcdef"          # the layout's example token
    assert not route(server)["known"]

    # The plan reads the device and the server and changes neither. It says what the apply
    # will do: enroll the device, write its client's settings, deliver the image.
    del server.requests[:]
    rc, out = play(run, server, "--check", "--diff")
    assert rc == 0, out
    assert f"route does not know '{BOARD}': to be enrolled" in out, out
    assert "zhb/server: set, zhb/port: set, zhb/id: set, zhb/token: set" in out and token not in out, out
    assert out.count("zhb/server: set") == 1, out                 # said once, not before and after
    assert '"version": "3.2.0"' in out and '"version": "3.3.0"' in out, out
    assert {m for m, _ in server.requests} == {"GET"} and not server.targets and not server.modules

    # The apply enrolls the device, gives its client its settings, and tells the server; the
    # device fetches the image, MCUboot runs it at the reset, the device confirms itself.
    rc, out = play(run, server)
    assert rc == 0, out
    assert token not in out
    record = route(server, credential=token)
    end = time.monotonic() + 60
    while record["open"] and time.monotonic() < end:      # the client reports once it is confirmed
        time.sleep(2)
        record = route(server, credential=token)
    assert (record["open"], record["result"], record["enrolled"]) == (False, "success", True), record
    assert record["image"] == {"name": "zqh-b", "version": "3.3.0"}
    # The image came from the server and from nowhere else: the device downloaded it.
    assert any(m == "GET" and p.endswith("/artifacts/app.signed.bin") for m, p in server.requests)

    # A second apply changes nothing, on the device or on the server.
    first = record["id"]
    rc, out = play(run, server)
    assert rc == 0 and "changed=0" in out, out
    assert route(server)["id"] == first


AS_BAD = ("-e", json.dumps({"firmware": {"family": "zephyr_settings", "image": "zqh-bad"}}))


@pytest.mark.skipif(not os.path.isfile(os.path.join(STORE, "zqh-bad", "manifest.json")),
                    reason="needs the image made to fail (just sim-zapp-qemu-build)")
def test_an_image_that_faults_on_its_test_boot_fails_the_apply_whatever_the_server_is_told(bench):
    """The device is brought to its declared image through the server, then given one made to
    fail: it faults a few seconds into its test boot, the device resets, and MCUboot returns to
    the image before it. The apply fails on what the device runs. The device's client is
    Zephyr's own, which then tells the server the update succeeded (the image it reports from
    is confirmed, and it finds the action still open): the run says what the server recorded,
    and where that is success, that the record is wrong."""
    run, server = bench
    rc, out = play(run, server)
    assert rc == 0, out

    rc, out = play(run, server, *AS_BAD)
    assert rc != 0, out
    assert "the device runs its previous image" in out, out
    # what the route recorded, said beside the device's answer: not yet heard, or wrong, or true
    assert "its hawkbit route" in out, out
    # the server did hand the bad image out: it failed on the device, not on the way to it
    assert any(m == "GET" and "/artifacts/app.signed.bin" in p for m, p in server.requests)
    record = route(server)
    end = time.monotonic() + 60
    while record["open"] and time.monotonic() < end:
        time.sleep(2)
        record = route(server)
    assert not record["open"] and record["image"] == {"name": "zqh-bad", "version": "3.4.0"}, record
    if record["result"] == "success":
        # the stock client's report: a second failed apply would say so outright; here the
        # device's answer already failed the run, which is the point
        assert rc != 0

    # The device is back on its declared image by itself: nothing to do.
    rc, out = play(run, server, "--check")
    assert rc == 0 and "changed=0" in out, out
