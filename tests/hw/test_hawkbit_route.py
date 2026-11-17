"""The hawkBit route on a real device (tests/hw/README.md, "The hawkBit route"): a device whose
layout pins `routes: {flash: hawkbit}`, a real server, and the tool run as a site runs it.

    RXRBENCH_HW_LAYOUT          the layout that pins the route
    RXRBENCH_HW_BOARDS          the device (one)
    RXRBENCH_HW_HAWKBIT         the server, as this machine reaches it (http://host:8080)
    RXRBENCH_HW_HAWKBIT_AUTH    user:password of its Management API (not kept anywhere)
    RXRBENCH_HW_APPLY=1         also deliver: the device takes its declared bundle and reboots
    RXRBENCH_HW_POLL            a command run every few seconds while a delivery is waited for,
                                for a device whose image has no timer for its update client
    RXRBENCH_HW_BUNDLE          a bundle to take as the device's declared one, in place of the
                                layout's (a test image; its version must be new to the server)

The plan reads the device and the server and writes to neither, and runs whenever the first
four are set. The delivery writes to the device (a bundle into its other slot, a reboot) and
runs only with RXRBENCH_HW_APPLY. Claim the device on whatever the bench shares first."""
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
USER, _, PASSWORD = os.environ.get("RXRBENCH_HW_HAWKBIT_AUTH", "").partition(":")
APPLY = bool(os.environ.get("RXRBENCH_HW_APPLY"))
POLL = os.environ.get("RXRBENCH_HW_POLL", "")
BUNDLE = os.environ.get("RXRBENCH_HW_BUNDLE", "")
SETTINGS = {"url": SERVER, "user": USER, "password": PASSWORD}

pytestmark = pytest.mark.skipif(
    len(BOARDS) != 1 or not LAYOUT or not SERVER or not USER,
    reason="needs one device in RXRBENCH_HW_BOARDS, RXRBENCH_HW_LAYOUT, RXRBENCH_HW_HAWKBIT and "
           "RXRBENCH_HW_HAWKBIT_AUTH")


def play(*args, timeout=3600):
    """apply.yml on the layout, for the one device, with the server of this run."""
    where = subprocess.run([os.path.join(VENV, "python"), os.path.join(REPO, "tools", "where.py"), LAYOUT],
                           cwd=REPO, capture_output=True, text=True, check=False)
    assert where.returncode == 0, where.stdout + where.stderr
    env = {**os.environ, "ANSIBLE_FORCE_COLOR": "0", "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", where.stdout.strip().splitlines()[-1],
                        os.path.join(PLAYS, "apply.yml"), "--limit", BOARDS[0],
                        "-e", json.dumps({"bench_hawkbit": SETTINGS,
                                          **({"bench_bundle": BUNDLE} if BUNDLE else {})}), *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout + r.stderr


def route(verb, target):
    r = subprocess.run([sys.executable, TOOL, verb], capture_output=True, text=True, timeout=120, check=False,
                       input=json.dumps({"settings": SETTINGS, "target": target}))
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 0, out
    return out


def address():
    """The device's address on the route, as the registry on this machine records it."""
    r = subprocess.run(["devreg", "resolve", "--route", "hawkbit", BOARDS[0]], capture_output=True, text=True,
                       check=False)
    if r.returncode != 0:
        pytest.skip(f"{BOARDS[0]}: {(r.stdout + r.stderr).strip()}")
    return r.stdout.strip().splitlines()[-1]


def test_the_plan_reads_the_device_and_the_server_and_delivers_nothing():
    target = address()
    before = route("state", target)
    assert before["known"], f"the server has no target {target!r}: {before['detail']}"
    rc, out = play("--check", "--diff")
    assert rc == 0, out
    after = route("state", target)
    assert after.get("id") == before.get("id") and after["open"] == before["open"], (before, after)


@pytest.mark.skipif(not APPLY, reason="a delivery writes to the device: asked for with RXRBENCH_HW_APPLY=1")
def test_the_device_takes_its_declared_bundle_through_the_server_and_the_record_is_true():
    target = address()
    stop = threading.Event()

    def timer():
        while not stop.wait(15):
            subprocess.run(shlex.split(POLL), capture_output=True, timeout=900, check=False)

    ticking = threading.Thread(target=timer, daemon=True)
    if POLL:
        ticking.start()
    try:
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
    finally:
        stop.set()
        if ticking.is_alive():
            ticking.join(60)
