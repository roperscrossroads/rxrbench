"""The Linux plugin against simulated devices: containers with an ssh server and nothing
else, reached through a registry and a layout as a real device is. Needs Docker; skipped
without it."""
import os
import shutil
import subprocess
import sys

import pytest

from ansible_collections.rxrbench.linux.sim import simlinux

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(REPO, "venv", "bin")
PLAYS = os.path.join("collections", "ansible_collections", "rxrbench", "core", "playbooks")
SIM = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "linux", "sim", "simlinux.py")
pytestmark = pytest.mark.skipif(not simlinux.available(), reason="needs Docker")


def sim(*argv):
    r = subprocess.run([sys.executable, SIM, *argv], cwd=REPO, capture_output=True, text=True,
                       timeout=600, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.fixture(scope="module")
def bench():
    """`just sim-linux-up`, and down again."""
    sim("up")
    yield
    sim("down")


def play(playbook, *args, env=None):
    e = {**os.environ, "ANSIBLE_FORCE_COLOR": "0", "PATH": VENV + os.pathsep + os.environ["PATH"],
         **(env or {})}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", "site.example/layouts/simlinux",
                        os.path.join(PLAYS, playbook), *args],
                       cwd=REPO, env=e, capture_output=True, text=True, timeout=300, check=False)
    return r.returncode, r.stdout + r.stderr


def recap(out, device):
    lines = [ln for ln in out.splitlines() if ln.startswith(device) and "changed=" in ln]
    assert lines, out
    return lines[-1]


def test_a_device_is_read_and_a_plan_and_an_apply_change_nothing(bench):
    rc, out = play("facts.yml")
    assert rc == 0, out
    for name in ("lin1", "lin2"):
        assert f"{name}: rxrbench simulated device 1 on " in out, out
    for args in (("--check", "--diff"), ()):
        rc, out = play("apply.yml", *args)
        assert rc == 0, out
        assert "changed=0" in recap(out, "lin1") and "failed=0" in recap(out, "lin2")


def test_a_device_that_is_not_what_is_declared_is_named_and_left_alone(bench):
    rc, out = play("apply.yml", "--limit", "lin2",
                   "-e", '{"linux": {"os": {"id": "rxrbench-sim", "version_id": "2"}}}')
    assert rc != 0
    assert "lin2 is not what the layout declares (version_id: it says 1, declared 2)" in out, out
    assert "re-image it" in out


def test_tests_and_diagnostics_run_over_ssh(bench, tmp_path):
    for playbook in ("test.yml", "diag.yml"):
        rc, out = play(playbook, "-e", f"bench_runs_dir={tmp_path}")
        assert rc == 0, out
        assert "failed=0" in recap(out, "lin1"), out
    kept = [os.path.join(d, f) for d, _dirs, files in os.walk(tmp_path) for f in files]
    assert any(p.endswith("os-release.txt") and "/lin1/diag/" in p for p in kept), kept
    assert any(p.endswith("/lin2/test.json") for p in kept), kept


def test_locate_blinks_and_gives_the_led_its_trigger_back(bench):
    rc, out = play("locate.yml", "--limit", "lin1", "-e", "locate_seconds=1")
    assert rc == 0, out
    led = subprocess.run(["docker", "exec", "rxrbench-sim-lin1", "cat", "/run/leds/act/trigger"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert led == "none", "the trigger it had before the blink"


def test_a_reboot_is_seen_over_ssh(bench):
    rc, out = play("reboot.yml", "--limit", "lin1")
    assert rc == 0, out
    assert "lin1: rebooted, back after" in out, out


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A site of its own, with an encrypted store only this test can read."""
    root = tmp_path / "site"
    root.mkdir()
    key = tmp_path / "age.txt"
    out = subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, text=True, check=True)
    public = (out.stderr + out.stdout).split("ublic key:")[1].split()[0]
    (root / ".sops.yaml").write_text(f"creation_rules:\n  - age: {public}\n")
    monkeypatch.setenv("RXRBENCH_SITE", str(root))
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    return root


def said(out, device, kind):
    lines = [ln for ln in out.splitlines() if f"{device}: {kind} (" in ln]
    assert lines, out
    return lines[-1]


@pytest.mark.skipif(not shutil.which("sops") or not shutil.which("age-keygen"), reason="needs sops and age")
def test_a_re_imaged_device_gets_its_host_key_back(bench, site):
    rc, out = play("secrets.yml")
    assert rc == 0, out
    assert "NOT ESCROWED" in said(out, "lin1", "host_key")
    assert "PRIVATE KEY" not in out, "the key is never shown"

    rc, out = play("keys-export.yml")
    assert rc == 0, out
    stored = (site / "identity" / "lin1.sops.yaml").read_text()
    assert "sops:" in stored and "ENC[" in stored and "PRIVATE KEY" not in stored

    rc, out = play("secrets.yml")
    assert rc == 0, out
    was = said(out, "lin1", "host_key")
    assert "matches the escrow" in was and "SHA256:" in was

    # the device comes back new: another host key, a stranger to the escrow
    sim("reimage", "lin1")
    rc, out = play("secrets.yml")
    assert rc == 0, out
    assert "DIFFERS from the escrow" in said(out, "lin1", "host_key")
    assert "matches the escrow" in said(out, "lin2", "host_key"), "the other device was not touched"

    rc, out = play("keys-restore.yml", "--check")
    assert rc == 0, out
    assert "changed=1" in recap(out, "lin1") and "changed=0" in recap(out, "lin2")
    rc, out = play("keys-restore.yml")
    assert rc == 0, out
    assert "changed=1" in recap(out, "lin1")
    # The device presents its escrowed key at once (the restore proved it on a connection
    # pinned to that key), so the pin of the key it had in between is replaced.
    sim("pin", "lin1")

    rc, out = play("secrets.yml")
    assert rc == 0, out
    assert said(out, "lin1", "host_key") == was, "the same key as before it was re-imaged"
    rc, out = play("keys-restore.yml")
    assert rc == 0 and "changed=0" in recap(out, "lin1"), out
