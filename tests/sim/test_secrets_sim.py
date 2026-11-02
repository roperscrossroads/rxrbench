"""Secrets on the simulated bench: what each board holds against the site's escrow and the
layout's declaration, and the escrow's round trip through the site's encrypted store. Needs
the sim boards built, and sops and age for the store; skipped without them."""
import os
import shutil
import subprocess

import pytest

from ansible_collections.rxrbench.mesh.sim import simboard
from tests.sim import test_sim_layout, test_zephcore_sim
from tests.sim.test_sim_layout import bench as sim_bench  # noqa: F401  (two boards of layout sim)
from tests.sim.test_zephcore_sim import bench as zc_bench  # noqa: F401  (two boards of layout zcsim)

REPO, VENV = test_sim_layout.REPO, test_sim_layout.VENV
pytestmark = pytest.mark.skipif(
    not simboard.available() or not shutil.which("sops") or not shutil.which("age-keygen"),
    reason="needs the sim board (just sim-build), sops and age")


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


def play(run, layout, playbook, *args):
    env = {**os.environ, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0",
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", f"site.example/layouts/{layout}",
                        os.path.join("collections/ansible_collections/rxrbench/core/playbooks", playbook), *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=300, check=False)
    return r.returncode, r.stdout + r.stderr


def said(out, board, kind):
    """The status line about one secret of one board."""
    lines = [ln for ln in out.splitlines() if f"{board}: {kind} (" in ln]
    assert lines, out
    return lines[-1]


def test_declared_secrets_are_compared_with_what_a_board_holds(sim_bench, site):  # noqa: F811
    rc, out = play(sim_bench, "sim", "secrets.yml")
    assert rc == 0, out
    before = said(out, "sim1", "channel_keys")
    assert "DIFFERS from what is declared" in said(out, "sim1", "admin_keys")
    rc, out = play(sim_bench, "sim", "apply.yml")
    assert rc == 0, out
    rc, out = play(sim_bench, "sim", "secrets.yml")
    assert rc == 0, out
    assert "as declared" in said(out, "sim1", "channel_keys"), before
    # the admin keys a layout lists, and a board whose layout lists none
    assert "1 admin key(s), as declared" in said(out, "sim1", "admin_keys")
    assert "not declared" in said(out, "sim2", "admin_keys")
    # a board that cannot give its key says so; nothing is guessed
    assert "identity (device)" in said(out, "sim1", "identity")


@pytest.mark.skipif(not os.access(test_zephcore_sim.IMAGE, os.X_OK),
                    reason="needs zephcore's sim image (just sim-build-zephcore), which has no "
                           "public source: a workstation runs it, CI does not")
def test_a_key_a_board_made_is_escrowed_compared_and_never_shown(zc_bench, site):  # noqa: F811
    rc, out = test_zephcore_sim.play(zc_bench)
    assert rc == 0, out

    rc, out = play(zc_bench, "zcsim", "secrets.yml")
    assert rc == 0, out
    assert "NOT ESCROWED" in said(out, "zc1", "identity")
    assert "as declared" in said(out, "zc1", "admin_password")
    assert "sim-pass" not in out, "the declared password is never shown"

    rc, out = play(zc_bench, "zcsim", "keys-export.yml")
    assert rc == 0, out
    assert "escrowed in identity/zc1.sops.yaml" in out
    stored = (site / "identity" / "zc1.sops.yaml").read_text()
    assert "sops:" in stored and "ENC[" in stored, "the store is encrypted"

    rc, out = play(zc_bench, "zcsim", "secrets.yml")
    assert rc == 0, out
    assert "matches the escrow" in said(out, "zc1", "identity")
    assert "matches the escrow" in said(out, "zc2", "identity")

    # a board that holds its key is left alone
    rc, out = play(zc_bench, "zcsim", "keys-restore.yml", "--check")
    assert rc == 0, out
    assert "changed=0" in [ln for ln in out.splitlines() if ln.startswith("zc1")][-1]
