"""The two A/B tiers of the Linux plugin against a simulated device: a QEMU machine with
RAUC slots, slotguard's health gate and an ssh server, whose bootloader logic is the real
boot script run between boots. One device, one story, because a boot costs a minute and a
half: read it, refuse what must be refused, update it, see the gate pass, change nothing the
second time. Needs the simulated A/B bench set up (simvm.py setup); skipped without it."""
import os
import subprocess
import sys
import time

import pytest

from ansible_collections.rxrbench.linux.sim import simvm

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(REPO, "venv", "bin")
PLAYS = os.path.join("collections", "ansible_collections", "rxrbench", "core", "playbooks")
SIM = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "linux", "sim", "simvm.py")
pytestmark = pytest.mark.skipif(not simvm.available(),
                                reason="the simulated A/B bench is not set up: " + ", ".join(simvm.missing()))


def sim(*argv, timeout=600):
    r = subprocess.run([sys.executable, SIM, *argv], cwd=REPO, capture_output=True, text=True,
                       timeout=timeout, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip().splitlines()[-1]


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    """A device in its factory state, running, its host key pinned; stopped afterwards."""
    sim("fresh", "ab1")
    out = open(tmp_path_factory.mktemp("simvm") / "simvm.out", "wb")  # pylint: disable=consider-using-with
    proc = subprocess.Popen([sys.executable, "-u", SIM, "up", "ab1"], cwd=REPO, stdout=out,
                            stderr=subprocess.STDOUT)
    try:
        sim("pin", "ab1")
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        out.close()


def play(layout, playbook, *args, timeout=600):
    env = {**os.environ, "ANSIBLE_FORCE_COLOR": "0", "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", f"site.example/layouts/{layout}",
                        os.path.join(PLAYS, playbook), *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout + r.stderr


def slots(out):
    lines = [ln for ln in out.splitlines() if "ab1: rxrbench-sim-qemuarm" in ln]
    assert lines, out
    return lines[-1]


def healthy(layout="simab", seconds=240):
    """Wait until the running slot has passed its health gate; the line that says so."""
    end, out = time.monotonic() + seconds, ""
    while time.monotonic() < end:
        rc, out = play(layout, "facts.yml")
        if rc == 0 and "health healthy" in slots(out):
            return slots(out)
        time.sleep(5)
    raise AssertionError(out)


def test_an_ab_device_is_updated_gated_and_left_alone_the_second_time(bench):
    update = sim("bundle", "--version", "1.1-sim")
    assert update.endswith("update-1.1-sim.raucb"), "the bundle the example layouts declare"

    # As it leaves the factory: slot A, no bundle recorded, and the gate passes after its soak.
    assert "on slot A (boot status good" in healthy()

    # A bundle for another device is refused before anything is copied.
    other = sim("bundle", "--version", "9-other", "--compatible", "some-other-device")
    rc, out = play("simab", "apply.yml", "--check", "-e", f"bench_bundle={other}")
    assert rc != 0 and "the bundle is for 'some-other-device'; the device is 'rxrbench-sim-qemuarm'" in out, out

    # A bundle signed by a key the device does not trust is refused by the device itself; it
    # stays on its slot and the bundle does not stay on it.
    foreign = sim("bundle", "--version", "9-foreign", "--foreign")
    rc, out = play("simab", "apply.yml", "-e", f"bench_bundle={foreign}")
    assert rc != 0 and "signature verification failed" in out, out
    rc, out = play("simab", "test.yml", "-e", '{"bench_tests": [{"name": "no-bundle-left", '
                   '"cmd": "ls /data/update.raucb 2>&1 || true", "expect": "No such file"}]}')
    assert rc == 0, out
    rc, out = play("simab", "facts.yml")
    assert rc == 0 and "on slot A" in slots(out), out

    # The plan says what would change; the apply fills the other slot, reboots into it (the
    # boot script chooses it), and waits for the health gate.
    rc, out = play("simab", "apply.yml", "--check", "--diff")
    assert rc == 0 and '"version": "1.1-sim"' in out and "changed=1" in out, out
    rc, out = play("simab", "apply.yml")
    assert rc == 0, out
    assert "rxrbench-sim-qemuarm 1.1-sim on slot B (boot status good, health healthy)" in slots(out), out

    # A second apply changes nothing, and the device passes its tests.
    rc, out = play("simab", "apply.yml")
    assert rc == 0 and "changed=0" in out, out
    rc, out = play("simab", "test.yml")
    assert rc == 0, out

    # The same device taken as plain A/B slots: no gate is asked, the update system's own
    # word is all there is, and nothing is to be done.
    rc, out = play("simabrauc", "apply.yml", "--check")
    assert rc == 0 and "changed=0" in out, out
    assert "1.1-sim on slot B (boot status good, health none)" in slots(out), out

    # A reboot over ssh is proven by its uptime, and it comes back on the slot it was on,
    # with the host key it had (the pin still holds, or nothing here would connect).
    rc, out = play("simab", "reboot.yml", "--limit", "ab1", "-e", "reboot_settle=240")
    assert rc == 0 and "ab1: rebooted, back after" in out, out
    rc, out = play("simab", "facts.yml")
    assert rc == 0 and "on slot B" in slots(out), out


def slot_of(out):
    return slots(out).split(" on slot ")[1][0]


def test_an_update_that_does_not_work_is_rolled_back_and_the_tool_says_so(bench):
    """Two validly signed bundles that do not work, on the device the first test left: one
    boots and fails its health gate, one panics before userspace. Each time the tool reports
    it, and the boot script's attempt counter takes the device back to the slot it had."""
    healthy()
    rc, out = play("simab", "facts.yml")
    home = slot_of(out)
    other = "B" if home == "A" else "A"

    # It boots, it answers, and its gate fails: the slot is never marked good.
    unhealthy = sim("bundle", "--version", "2-sim", "--broken", "unhealthy")
    rc, out = play("simab", "apply.yml", "-e", f"bench_bundle={unhealthy}")
    assert rc != 0, out
    assert (f"the device runs the update on slot {other} but it FAILED its health check: it is not "
            f"marked good and will fail over to slot {home}") in out, out
    # ... and it does: each further boot uses one of the slot's attempts, then the old slot.
    for _ in range(3):
        rc, out = play("simab", "reboot.yml", "--limit", "ab1", "-e", "reboot_settle=240")
        assert rc == 0, out
        rc, out = play("simab", "facts.yml")
        assert rc == 0, out
        if slot_of(out) == home:
            break
    assert slot_of(out) == home, out
    healthy()

    # It never reaches userspace: the device is back on its old slot before the tool sees it
    # again, and the tool says the update did not take.
    noinit = sim("bundle", "--version", "3-sim", "--broken", "noinit")
    rc, out = play("simab", "apply.yml", "-e", f"bench_bundle={noinit}")
    assert rc != 0, out
    assert f"the device booted slot {home} again: the update was installed but not booted, or it was rolled back" in out, out
    rc, out = play("simab", "facts.yml")
    assert rc == 0 and slot_of(out) == home, out
