"""The hawkBit route on a real device (tests/hw/README.md, "The hawkBit route"): a device whose
layout pins `routes: {flash: hawkbit}`, a real server, and the tool run as a site runs it.

    RXRBENCH_HW_LAYOUT          the layout that pins the route
    RXRBENCH_HW_BOARDS          the device (one)
    RXRBENCH_HW_HAWKBIT         the server, as this machine reaches it (http://host:8080), or
                                `standin:<address>:<port>`: the plugin's stand-in server,
                                started by this test on that address of this machine (the
                                one the boards' client config names) and stopped with it
    RXRBENCH_HW_HAWKBIT_AUTH    user:password of its Management API (not kept anywhere;
                                not needed for the stand-in)
    RXRBENCH_HW_APPLY=1         also deliver: the device takes its declared bundle and reboots
    RXRBENCH_HW_POLL            a command run every few seconds while a delivery is waited for,
                                for a device whose image has no timer for its update client
    RXRBENCH_HW_BUNDLE          a bundle to take as the device's declared one, in place of the
                                layout's (a test image; its version must be new to the server)
    RXRBENCH_HW_BAD_LAYOUT      a second layout of the same device that declares an image made
                                to fail on it (one that faults on its test boot, a bundle
                                whose slot fails its health gate): with RXRBENCH_HW_APPLY, the
                                failure is taken through the route too

The plan reads the device and the server and writes to neither, and runs whenever the first
four are set. The delivery writes to the device (a bundle into its other slot, a reboot) and
runs only with RXRBENCH_HW_APPLY. Claim the device on whatever the bench shares first.

With the stand-in the server starts empty every time: the layout must declare the device's
token (bench_hawkbit_token), so that the run enrolls it, and the device must hold the same."""
import contextlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(REPO, "venv", "bin")
PLAYS = os.path.join("collections", "ansible_collections", "rxrbench", "core", "playbooks")
TOOL = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "hawkbit", "host", "bin",
                    "hawkbit_route.py")
BOARDS = [b for b in os.environ.get("RXRBENCH_HW_BOARDS", "").split(",") if b]
LAYOUT = os.environ.get("RXRBENCH_HW_LAYOUT", "")
SERVER = os.environ.get("RXRBENCH_HW_HAWKBIT", "")
STANDIN = SERVER.startswith("standin:")
USER, _, PASSWORD = os.environ.get("RXRBENCH_HW_HAWKBIT_AUTH", "bench:made-up" if STANDIN else "").partition(":")
BAD_LAYOUT = os.environ.get("RXRBENCH_HW_BAD_LAYOUT", "")
APPLY = bool(os.environ.get("RXRBENCH_HW_APPLY"))
POLL = os.environ.get("RXRBENCH_HW_POLL", "")
BUNDLE = os.environ.get("RXRBENCH_HW_BUNDLE", "")
SETTINGS = {"url": SERVER, "user": USER, "password": PASSWORD}


@pytest.fixture(scope="module", autouse=True)
def standin():
    """The stand-in server, for a run that asks for it: on this machine's address on the
    boards' network, turning away a device that shows another token than its target's."""
    if not STANDIN:
        yield None
        return
    sys.path.insert(0, os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "hawkbit", "sim"))
    import fakeserver  # pylint: disable=import-outside-toplevel,import-error
    _, host, port = SERVER.split(":")
    # devices are told to ask every half minute, not every five: a test waits on each one
    with fakeserver.FakeHawkbit(bind=host, port=int(port), token_auth=True, polling="00:00:30") as hb:
        SETTINGS["url"] = hb.url
        yield hb

pytestmark = pytest.mark.skipif(
    len(BOARDS) != 1 or not LAYOUT or not SERVER or not USER,
    reason="needs one device in RXRBENCH_HW_BOARDS, RXRBENCH_HW_LAYOUT, RXRBENCH_HW_HAWKBIT and "
           "RXRBENCH_HW_HAWKBIT_AUTH")


def play(*args, layout=None, timeout=3600):
    """apply.yml on the layout, for the one device, with the server of this run."""
    where = subprocess.run([os.path.join(VENV, "python"), os.path.join(REPO, "tools", "where.py"),
                            layout or LAYOUT],
                           cwd=REPO, capture_output=True, text=True, check=False)
    assert where.returncode == 0, where.stdout + where.stderr
    env = {**os.environ, "ANSIBLE_FORCE_COLOR": "0", "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", where.stdout.strip().splitlines()[-1],
                        os.path.join(PLAYS, "apply.yml"), "--limit", BOARDS[0],
                        "-e", json.dumps({"bench_hawkbit": SETTINGS,
                                          # the bad layout's image is its own
                                          **({"bench_bundle": BUNDLE} if BUNDLE and not layout else {})}),
                        *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout + r.stderr


def route(verb, target):
    r = subprocess.run([sys.executable, TOOL, verb], capture_output=True, text=True, timeout=120, check=False,
                       input=json.dumps({"settings": SETTINGS, "target": target}))
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 0, out
    return out


def address():
    """The device's address on the route, as the registry records it (this checkout's devreg;
    DEVREG_REGISTRY names a registry other than the site's)."""
    r = subprocess.run([os.path.join(VENV, "python"), os.path.join(REPO, "devreg", "devreg.py"), "resolve",
                        "--route", "hawkbit", BOARDS[0]], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        pytest.skip(f"{BOARDS[0]}: {(r.stdout + r.stderr).strip()}")
    return r.stdout.strip().splitlines()[-1]


@contextlib.contextmanager
def polling():
    """RXRBENCH_HW_POLL run every few seconds, for a device whose image has no timer for its
    update client: the test stands in for the timer and for nothing else."""
    stop = threading.Event()

    def timer():
        while not stop.wait(15):
            subprocess.run(shlex.split(POLL), capture_output=True, timeout=900, check=False)

    ticking = threading.Thread(target=timer, daemon=True)
    if POLL:
        ticking.start()
    try:
        yield
    finally:
        stop.set()
        if ticking.is_alive():
            ticking.join(60)


def test_the_plan_reads_the_device_and_the_server_and_delivers_nothing():
    """A server that has never heard of the device is fine when the layout declares the
    device's token: the plan then says the run will enroll it, and still tells the server
    nothing."""
    target = address()
    before = route("state", target)
    rc, out = play("--check", "--diff")
    assert rc == 0, out
    after = route("state", target)
    if not before["known"]:
        assert "to be enrolled" in out, f"the server has no target {target!r} and the plan enrolls none:\n{out}"
    assert after["known"] == before["known"], (before, after)
    assert after.get("id") == before.get("id") and after["open"] == before["open"], (before, after)


@pytest.mark.skipif(not APPLY, reason="a delivery writes to the device: asked for with RXRBENCH_HW_APPLY=1")
def test_the_device_takes_its_declared_bundle_through_the_server_and_the_record_is_true():
    target = address()
    with polling():
        rc, out = play("--diff")
        assert rc == 0, out
        # The device's own client reports after the reboot and its health gate: the server's
        # record is waited for, and must say what the device says.
        end = time.monotonic() + 900
        state = route("state", target)
        while state["open"] and time.monotonic() < end:
            time.sleep(10)
            state = route("state", target)
        assert (state["open"], state["result"]) == (False, "success"), state
        # A second run finds nothing to do, on the device or on the server.
        rc, out = play("--diff")
        assert rc == 0 and "changed=0" in out, out
        assert route("state", target).get("id") == state.get("id")


@pytest.mark.skipif(not (APPLY and BAD_LAYOUT),
                    reason="needs RXRBENCH_HW_APPLY=1 and RXRBENCH_HW_BAD_LAYOUT, a layout of the device "
                           "that declares an image made to fail on it")
def test_an_image_that_fails_on_the_device_fails_the_apply_and_the_server_hears_it():
    """The device is on its declared image (the test above, or an apply here). The bad image
    goes through the server like any other; the device takes it, it fails there, and the
    device returns to what it ran. The apply must fail on what the device says, and the
    server's record must say failure too: a client that reports before it knows, or one that
    calls a swap back a success, leaves the server saying success, and fails this test."""
    target = address()
    with polling():
        rc, out = play("--diff")
        assert rc == 0, out
        rc, out = play("--diff", layout=BAD_LAYOUT)
        assert rc != 0, f"the apply of an image made to fail succeeded:\n{out[-3000:]}"
        end = time.monotonic() + 900
        state = route("state", target)
        while state["open"] and time.monotonic() < end:
            time.sleep(10)
            state = route("state", target)
        assert not state["open"], state
        # Back on its declared image by itself: a plan of the good layout finds nothing to do.
        # (A device with A/B slots may need several boots to use up the bad slot's attempts.)
        end = time.monotonic() + 1500
        while True:
            rc, out = play("--check", "--diff")
            if rc == 0 and "changed=0" in out:
                break
            assert time.monotonic() < end, f"the device did not return to its declared image:\n{out[-3000:]}"
            time.sleep(20)
        # Last, so that a client that misreports does not hide what the device did.
        assert state["result"] == "failure", (
            "the apply failed and the device is back on its declared image, but the server's "
            "record says success for the update it did not keep: the device's client reports "
            f"before it knows, or calls a swap back a success: {state}")
