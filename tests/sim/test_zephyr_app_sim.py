"""The PC bench with what a device keeps (docs/SIMULATED.md: the zephyr plugin's
application on two of the four simulated boards (`site.example/layouts/zsim`), its declared
settings and secrets written through the settings shell and kept out of the broker's log,
the device-made key escrowed to an encrypted store and given back after an erase, an
upgrade and a downgrade that keep the settings, the chip (the flash file) backed up and
put back. Needs the images (`just sim-zephyr-build`, `just sim-zapp-build`), sops and age;
skipped without."""
import json
import os
import shutil
import subprocess

import pytest

from tests.sim import test_zephyr_sim
from tests.sim.test_zephyr_sim import bench, recap  # noqa: F401  (the four boards)

REPO, VENV, STORE = test_zephyr_sim.REPO, test_zephyr_sim.VENV, test_zephyr_sim.STORE
PB = "collections/ansible_collections/rxrbench/core/playbooks"

pytestmark = pytest.mark.skipif(
    not all(os.access(os.path.join(STORE, n, "zephyr.exe"), os.X_OK)
            for n in ("zephyr-shell", "zephyr-shell-b", "zapp-1.0", "zapp-1.1", "zapp-2.0"))
    or not shutil.which("sops") or not shutil.which("age-keygen"),
    reason="needs the images (just sim-zephyr-build, just sim-zapp-build), sops and age")


@pytest.fixture
def site(tmp_path):
    """A site of its own with an encrypted store only this test can read; the layouts are the
    examples' (a site without them falls back to those), the plugins the examples'."""
    root = tmp_path / "site"
    root.mkdir()
    key = tmp_path / "age.txt"
    out = subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, text=True, check=True)
    public = (out.stderr + out.stdout).split("ublic key:")[1].split()[0]
    (root / ".sops.yaml").write_text(f"creation_rules:\n  - age: {public}\n")
    shutil.copy(os.path.join(REPO, "site.example", "plugins.yml"), root / "plugins.yml")
    return {"RXRBENCH_SITE": str(root), "SOPS_AGE_KEY_FILE": str(key)}


def play(run, site, playbook, *args, timeout=300):  # noqa: F811  pylint: disable=redefined-outer-name
    env = {**os.environ, **site, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0",
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", "site.example/layouts/zsim",
                        os.path.join(PB, playbook), *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout + r.stderr


def said(out, board, kind):
    """The status line about one secret of one board."""
    lines = [ln for ln in out.splitlines() if f"{board}: {kind} (" in ln]
    assert lines, out
    return lines[-1]


def at(version):
    return ("--limit", "zb3", "-e", json.dumps({"firmware": {"family": "zephyr_settings", "image": f"zapp-{version}"}}))


def log_of(run, board):
    with open(os.path.join(run, f"{board}.log"), "rb") as f:
        return f.read()


def test_declared_settings_and_secrets_are_written_once_and_kept_out_of_the_log(bench, site):  # noqa: F811
    # A plan names what differs, by name only; the apply writes it; a second plan is clean.
    rc, out = play(bench, site, "apply.yml", "--check", "--diff", "--limit", "zb3,zb4")
    assert rc == 0, out
    assert "zb3: app/psk: set, tls/cert: set, net/name: set" in out, out
    assert "psk-zb3-0123" not in out and "MIIBsz" not in out, out
    rc, out = play(bench, site, "apply.yml", "--limit", "zb3,zb4")
    assert rc == 0 and recap(out)["zb3"]["changed"] == 2 and recap(out)["zb4"]["failed"] == 0, out
    rc, out = play(bench, site, "apply.yml", "--check", "--limit", "zb3,zb4")
    assert rc == 0 and recap(out)["zb3"]["changed"] == 0 and recap(out)["zb4"]["changed"] == 0, out
    # The device tests read the settings back; the secrets' status is as declared.
    rc, out = play(bench, site, "test.yml", "--limit", "zb3,zb4")
    assert rc == 0 and "FAIL" not in out, out
    rc, out = play(bench, site, "secrets.yml", "--limit", "zb3")
    assert rc == 0, out
    assert "as declared" in said(out, "zb3", "psk") and "as declared" in said(out, "zb3", "cert"), out
    assert "NOT ESCROWED" in said(out, "zb3", "device_key"), out
    # Nothing secret in the broker's log: not the psk, not the certificate (echoed wrapped at
    # the shell's terminal width), not the 64-hex key; the shell's echo of a write is redacted.
    log = log_of(bench, "zb3")
    assert b"psk-zb3-0123" not in log and b"MIIBsz" not in log, log[-2000:]
    assert not any(len(w) == 64 and all(c in b"0123456789abcdef" for c in w) for w in log.split()), log[-2000:]
    assert b"settings write string app/psk <secret>" in log, log[-2000:]


def test_the_device_key_is_escrowed_and_given_back_after_an_erase(bench, site):  # noqa: F811
    rc, out = play(bench, site, "keys-export.yml", "--limit", "zb3")
    assert rc == 0 and "escrowed in identity/zb3.sops.yaml" in out, out
    assert os.path.isfile(os.path.join(site["RXRBENCH_SITE"], "identity", "zb3.sops.yaml"))
    rc, out = play(bench, site, "secrets.yml", "--limit", "zb3")
    assert "matches the escrow" in said(out, "zb3", "device_key"), out
    before = said(out, "zb3", "device_key").split()[3]   # the key's id
    # An erase: the board makes a new key, and its declared settings are gone.
    rc, out = play(bench, site, "erase-chip.yml", "--limit", "zb3", "-e", "erase_without_backup=true")
    assert rc == 0 and "the flash file erased" in out, out
    rc, out = play(bench, site, "secrets.yml", "--limit", "zb3")
    assert "DIFFERS from the escrow" in said(out, "zb3", "device_key"), out
    assert "DIFFERS from what is declared" in said(out, "zb3", "psk"), out
    # The escrowed key written back: the device holds its old key again.
    rc, out = play(bench, site, "keys-restore.yml", "--limit", "zb3")
    assert rc == 0 and recap(out)["zb3"]["changed"] == 1, out
    rc, out = play(bench, site, "secrets.yml", "--limit", "zb3")
    assert "matches the escrow" in said(out, "zb3", "device_key"), out
    assert said(out, "zb3", "device_key").split()[3] == before, out


def test_an_upgrade_and_a_downgrade_keep_the_settings_and_the_key(bench, site):  # noqa: F811
    rc, out = play(bench, site, "apply.yml", "--limit", "zb3")
    assert rc == 0, out
    rc, out = play(bench, site, "keys-export.yml", "--limit", "zb3")
    assert rc == 0, out
    for version in ("1.1", "2.0", "1.0"):
        rc, out = play(bench, site, "apply.yml", "--diff", *at(version))
        assert rc == 0 and recap(out)["zb3"]["changed"] == 1, (version, out)   # the image, nothing else
        rc, out = play(bench, site, "test.yml", *at(version))
        assert rc == 0 and "FAIL" not in out, (version, out)                  # app/version reads as declared
        rc, out = play(bench, site, "secrets.yml", *at(version))
        assert "matches the escrow" in said(out, "zb3", "device_key"), (version, out)
        assert "as declared" in said(out, "zb3", "psk"), (version, out)


def test_the_chip_is_backed_up_and_put_back(bench, site):  # noqa: F811
    rc, out = play(bench, site, "apply.yml", "--limit", "zb3")
    assert rc == 0, out
    # (the backup lands where a bench host keeps them, ~/rxrbench/backups, and a copy under
    # the repository's backups/; this is the bench host here)
    rc, out = play(bench, site, "backup.yml", "--limit", "zb3")
    assert rc == 0 and "chip 20" in out and "sim:zb3" in out, out
    # A change after the backup: another name, through the plan's own step.
    other = ("-e", json.dumps({"zephyr": {"device_key": "app/key", "settings": {"net/name": "other"},
                                          "secrets": {}}}))
    rc, out = play(bench, site, "apply.yml", "--limit", "zb3", *other)
    assert rc == 0 and "zb3: net/name: set" in out, out
    rc, out = play(bench, site, "apply.yml", "--check", "--limit", "zb3")
    assert "zb3: net/name: set" in out, out
    # The backup written back: the name is the declared one again, and nothing else changed.
    rc, out = play(bench, site, "restore-chip.yml", "--limit", "zb3")
    assert rc == 0 and recap(out)["zb3"]["changed"] == 1, out
    # the report names the backup and says only what is known (no empty `; ;` fields)
    assert "zb3: backup " in out and "; ;" not in out, out
    rc, out = play(bench, site, "apply.yml", "--check", "--limit", "zb3")
    assert rc == 0 and recap(out)["zb3"]["changed"] == 0, out
    rc, out = play(bench, site, "test.yml", "--limit", "zb3")
    assert rc == 0 and "FAIL" not in out, out


def test_a_certificate_longer_than_the_shell_shows_is_kept_by_the_firmwares_digest(bench, site):  # noqa: F811
    """A 504-byte certificate (the size a Pico 2 W's settings were refused at) is written and
    checked through zapp's `zapp digest`, which prints a setting's SHA-256; without it the plan
    refuses it, and the secret never reaches the broker's log either way."""
    cert = "MIIC" + "q" * 500
    zephyr = {"device_key": "app/key", "settings": {"tls/cert": cert, "net/name": "zb3"},
              "secrets": {"cert": "tls/cert"}, "facts": ["app/version", "net/name"]}
    plain = ("--limit", "zb3", "-e", json.dumps({"zephyr": zephyr}))
    rc, out = play(bench, site, "apply.yml", "--check", *plain)
    assert rc != 0 and "504 bytes" in out, out
    digest = ("--limit", "zb3", "-e", json.dumps({"zephyr": {**zephyr, "digest_command": "zapp digest"}}))
    rc, out = play(bench, site, "apply.yml", *digest)
    assert rc == 0 and recap(out)["zb3"]["failed"] == 0, out
    rc, out = play(bench, site, "apply.yml", "--check", *digest)
    assert rc == 0 and recap(out)["zb3"]["changed"] == 0, out
    rc, out = play(bench, site, "secrets.yml", *digest)
    assert rc == 0 and "as declared" in said(out, "zb3", "cert"), out
    assert b"qqqqqqqq" not in log_of(bench, "zb3")
