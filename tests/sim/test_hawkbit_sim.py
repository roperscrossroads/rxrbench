"""The hawkBit route against a simulated device: the Linux plugin's QEMU A/B machine with RAUC,
slotguard's health gate and the real boot script between boots, taking its bundle from an
update server instead of having it pushed. The device runs the plugin's own client
(hawkbit/device/slotguard-hawkbit), the server is a stand-in started here (or a real one:
RXRBENCH_TEST_HAWKBIT=http://host:8080, with RXRBENCH_TEST_HAWKBIT_AUTH=user:password), and the
tool is run as a site runs it: `apply.yml` on a layout that pins `routes: {flash: hawkbit}`.

The image has no unit for the client, so a thread here is its timer: it runs `poll` on the
device every few seconds, which is all a timer does.

One device, one story, because a boot costs a minute and a half. Needs the simulated A/B
bench set up (simvm.py setup); skipped without it."""
import json
import os
import subprocess
import sys
import threading
import time
import uuid

import pytest
import yaml

from ansible_collections.rxrbench.hawkbit.sim import fakeserver
from ansible_collections.rxrbench.linux.sim import simvm

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(REPO, "venv", "bin")
RX = os.path.join(REPO, "collections", "ansible_collections", "rxrbench")
PLAYS = os.path.join("collections", "ansible_collections", "rxrbench", "core", "playbooks")
SIM = os.path.join(RX, "linux", "sim", "simvm.py")
REGISTRY = os.path.join(RX, "hawkbit", "sim", "registry.yaml")
TOOL = os.path.join(RX, "hawkbit", "host", "bin", "hawkbit_route.py")
CLIENT = os.path.join(RX, "hawkbit", "device", "slotguard-hawkbit")
LAYOUT, DEVICE = "simabhawkbit", "hb1"
# The address QEMU's user network gives the machine it runs on: a constant of QEMU, not of any site.
QEMU_HOST = "10.0.2.2"  # privacy: allow
pytestmark = pytest.mark.skipif(not simvm.available(),
                                reason="the simulated A/B bench is not set up: " + ", ".join(simvm.missing()))


def sim(*argv, timeout=600):
    r = subprocess.run([sys.executable, SIM, "--registry", REGISTRY, *argv], cwd=REPO, capture_output=True,
                       text=True, timeout=timeout, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip().splitlines()[-1]


def on_device(command, stdin=None, timeout=60):
    """(exit code, output) of a command on the device, over the route the registry records."""
    with open(REGISTRY, encoding="utf-8") as fh:
        ssh = yaml.safe_load(fh)["devices"][DEVICE]["reach"]["ssh"]
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-p", str(ssh["port"]),
             "-i", os.path.expanduser(ssh["identity"]),
             "-o", f"UserKnownHostsFile={os.path.expanduser(ssh['known_hosts'])}",
             f"{ssh['user']}@{ssh['ip']}", command],
            input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, ""
    return r.returncode, r.stdout + r.stderr


class Server:
    """The update server of the story: the stand-in, or a real one named in the environment.
    `url` is where this machine finds it, `device_url` where the simulated device does
    (QEMU_HOST: this machine's loopback, from inside the device)."""

    def __init__(self):
        real = os.environ.get("RXRBENCH_TEST_HAWKBIT")
        self.fake = None if real else fakeserver.FakeHawkbit().__enter__()
        self.url = real or self.fake.url
        user, _, password = (os.environ.get("RXRBENCH_TEST_HAWKBIT_AUTH") or "bench:made-up").partition(":")
        self.settings = {"url": self.url, "user": user, "password": password}
        port = self.url.rsplit(":", 1)[1].split("/")[0]
        self.device_url = f"http://{QEMU_HOST}:{port}"
        self.target = DEVICE        # the device's address on the route, as the registry has it
        self.bundle = None          # the bundle the layout is taken to declare (play)
        # A version of this run's own: a real server keeps every image it was given, and will
        # not take a second one under a version it has (a bundle made twice is two bundles).
        self.run = uuid.uuid4().hex[:6]
        self.made = []
        if real:
            # A real server keeps what earlier runs left: the target is made anew, so that it
            # holds this run's token and no old action.
            import base64  # pylint: disable=import-outside-toplevel
            import urllib.error  # pylint: disable=import-outside-toplevel
            import urllib.request  # pylint: disable=import-outside-toplevel
            req = urllib.request.Request(
                f"{self.url}/rest/v1/targets/{DEVICE}", method="DELETE",
                headers={"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()})
            try:
                urllib.request.urlopen(req, timeout=30).close()  # pylint: disable=consider-using-with
            except urllib.error.HTTPError as e:
                assert e.code == 404, e

    def tool(self, verb, **request):
        r = subprocess.run([sys.executable, TOOL, verb], capture_output=True, text=True, timeout=120, check=False,
                           input=json.dumps({"settings": self.settings, "target": self.target, **request}))
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert r.returncode == 0, out
        return out

    def state(self):
        return self.tool("state")

    def close(self):
        if self.fake:
            self.fake.__exit__(None, None, None)


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    """The device in its factory state and running, a target on the server with the token the
    device holds, the client and its config on the device's /data, and the client's timer."""
    server = Server()
    sim("fresh", DEVICE)
    out = open(tmp_path_factory.mktemp("simvm") / "simvm.out", "wb")  # pylint: disable=consider-using-with
    proc = subprocess.Popen([sys.executable, "-u", SIM, "--registry", REGISTRY, "up", DEVICE], cwd=REPO,
                            stdout=out, stderr=subprocess.STDOUT)
    stop = threading.Event()

    def timer():
        while not stop.wait(4):
            on_device("sh /data/hawkbit/slotguard-hawkbit poll", timeout=300)

    ticking = threading.Thread(target=timer, daemon=True)
    try:
        sim("pin", DEVICE)
        token = uuid.uuid4().hex
        server.tool("target", token=token)
        with open(CLIENT, encoding="utf-8") as fh:
            rc, said = on_device("mkdir -p /data/hawkbit && cat > /data/hawkbit/slotguard-hawkbit", stdin=fh.read())
        assert rc == 0, said
        rc, said = on_device("cat > /data/hawkbit/config && chmod 600 /data/hawkbit/config",
                             stdin=f"SERVER={server.device_url}\nCONTROLLER_ID={server.target}\n"
                                   f"TARGET_TOKEN={token}\nREBOOT_ON_UNHEALTHY=1\n")
        assert rc == 0, said
        ticking.start()
        yield server
    finally:
        stop.set()
        if ticking.is_alive():
            ticking.join(30)
        proc.terminate()
        proc.wait(timeout=30)
        out.close()
        server.close()
        for path in server.made:            # this run's bundles are no later run's
            if os.path.exists(path):
                os.unlink(path)


def play(server, playbook, *args, bundle=None, timeout=900):
    env = {**os.environ, "ANSIBLE_FORCE_COLOR": "0", "PATH": VENV + os.pathsep + os.environ["PATH"]}
    # The server of this run in place of the layout's example, and this test's own bundle in
    # place of the one the example layouts share: another test may be making that one anew
    # for its own device at the same moment, and a bundle made twice is two bundles.
    extra = {"bench_hawkbit": server.settings, "bench_bundle": bundle or server.bundle}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", f"site.example/layouts/{LAYOUT}",
                        os.path.join(PLAYS, playbook), "-e", json.dumps(extra), *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout + r.stderr


def slots(out):
    lines = [ln for ln in out.splitlines() if f"{DEVICE}: rxrbench-sim-qemuarm" in ln]
    assert lines, out
    return lines[-1]


def slot_of(out):
    return slots(out).split(" on slot ")[1][0]


def healthy(server, seconds=300):
    """Wait until the running slot has passed its health gate; the line that says so."""
    end, out = time.monotonic() + seconds, ""
    while time.monotonic() < end:
        rc, out = play(server, "facts.yml")
        if rc == 0 and "health healthy" in slots(out):
            return slots(out)
        time.sleep(5)
    raise AssertionError(out)


def settled(server, seconds=120):
    """The server's record once the device's client has reported the outcome."""
    end = time.monotonic() + seconds
    state = server.state()
    while state["open"] and time.monotonic() < end:
        time.sleep(3)
        state = server.state()
    return state


def test_a_bundle_goes_through_the_server_and_the_device_is_read_as_ever(bench):
    server = bench
    good = f"1.1-hb{server.run}"
    server.bundle = sim("bundle", "--version", good)
    server.made.append(server.bundle)
    assert "on slot A (boot status good" in healthy(server)
    assert server.state()["known"] and not server.state()["open"]

    # The plan reads the device and the server and changes neither.
    if server.fake:
        del server.fake.requests[:]
    rc, out = play(server, "apply.yml", "--check", "--diff")
    assert rc == 0 and f'"version": "{good}"' in out and "changed=1" in out, out
    if server.fake:
        assert server.fake.requests and {m for m, _ in server.fake.requests} == {"GET"}
        assert not server.fake.modules and not server.fake.actions

    # The apply tells the server; the device fetches the bundle, installs it and reboots by
    # itself; the slot it then runs and its health gate are read over ssh.
    rc, out = play(server, "apply.yml")
    assert rc == 0, out
    assert f"rxrbench-sim-qemuarm {good} on slot B (boot status good, health healthy)" in slots(out), out
    # Nothing was copied to the place a pushed bundle lands.
    rc, said = on_device("ls /data/update.raucb 2>&1 || true")
    assert "No such file" in said, said
    # The device's client reported after the gate, so the server's record is true.
    record = settled(server)
    assert (record["open"], record["result"]) == (False, "success"), record
    assert record["image"] == {"name": "rxrbench-sim-qemuarm", "version": good}
    first = record["id"]

    # A second apply changes nothing, on the device or on the server.
    rc, out = play(server, "apply.yml")
    assert rc == 0 and "changed=0" in out, out
    assert server.state()["id"] == first


def test_a_bundle_that_fails_its_gate_fails_the_apply_and_the_server_hears_it(bench):
    """A validly signed bundle whose slot boots and fails its health gate, on the device the
    first test left. The apply fails on what the device says; the device's client reports the
    failure to the server and reboots the bad slot until the boot script returns to the old."""
    server = bench
    healthy(server)
    rc, out = play(server, "facts.yml")
    home = slot_of(out)
    unhealthy = sim("bundle", "--version", f"2-hb{server.run}", "--broken", "unhealthy")
    server.made.append(unhealthy)
    rc, out = play(server, "apply.yml", bundle=unhealthy)
    assert rc != 0, out
    record = settled(server, 240)
    assert (record["open"], record["result"]) == (False, "failure"), record
    assert "health gate failed" in record["detail"], record

    # The client uses up the bad slot's boot attempts; then the device is on its old slot,
    # healthy, and its client is offered nothing more.
    end = time.monotonic() + 900
    while time.monotonic() < end:
        rc, out = play(server, "facts.yml")
        if rc == 0 and slot_of(out) == home and "health healthy" in slots(out):
            break
        time.sleep(10)
    assert rc == 0 and slot_of(out) == home, out
    rc, said = on_device("sh /data/hawkbit/slotguard-hawkbit status")
    assert rc == 0 and "pending     none" in said, said
    assert server.state()["id"] == record["id"]            # no second delivery was made
    # The plan says again what the layout declares, and that the device does not run it.
    rc, out = play(server, "apply.yml", "--check", bundle=unhealthy)
    assert rc == 0 and "changed=1" in out, out
