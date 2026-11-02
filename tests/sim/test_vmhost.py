"""The bench on a PC, whole: the simulated bench host in QEMU, given the default install and
the sim bench with the layout's boards, then planned, applied and tested as a real bench is
(`site.example/layouts/zsim-vm`). Needs QEMU, the fetched image (`just sim-host-setup`) and
every image the layout's boards name in the registry (`sim_exe`: the sim-*-build recipes);
skipped without, naming what is missing. Slow: a
boot, the default install, the host setup (minutes with KVM). Without usable KVM the host runs
under TCG and the host setup alone outlasts a play's 15 minutes, so the test skips and says so
(on WSL2: nested virtualization in `.wslconfig`); `just sim-host-up` still works under TCG."""
import os
import subprocess
import sys

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(REPO, "venv", "bin")
SIM = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "sim")
STORE = os.path.join(REPO, ".build", "store")
LAYOUT = os.path.join(REPO, "site.example", "layouts", "zsim-vm")
STATE = os.path.expanduser(os.environ.get("RXRBENCH_VMHOST_DIR", "~/.cache/rxrbench/vmhost"))
NAME, PORT = "vmtest", 2322

sys.path.insert(0, os.path.join(REPO, "collections"))
from ansible_collections.rxrbench.core.sim import vmhost  # noqa: E402  pylint: disable=wrong-import-position



def missing_images():
    """The images the layout's boards name in its registry (`sim_exe`), and the shell board's
    second build the test applies, that are not in the image store."""
    with open(os.path.join(LAYOUT, "devreg.yml")) as f:
        layout = yaml.safe_load(f)
    with open(os.path.normpath(os.path.join(LAYOUT, layout["registry"]))) as f:
        devices = yaml.safe_load(f)["devices"]
    want = {(devices.get(b) or {}).get("sim_exe") for b in layout["boards"]} - {None}
    want.add("zephyr/zephyr-shell-b/zephyr.exe")
    return sorted(w for w in want if not os.path.exists(os.path.join(STORE, w)))


MISSING = missing_images()
pytestmark = pytest.mark.skipif(
    bool(vmhost.missing()) or not os.path.isfile(os.path.join(STATE, "base.img")) or bool(MISSING),
    reason="no QEMU, or the simulated host's image (just sim-host-setup), or images in .build/store: "
           + (", ".join(MISSING) or "none missing"))
pytestmark = [pytestmark, pytest.mark.skipif(
    not vmhost.have_kvm(),
    reason="no usable KVM: under TCG the host setup outlasts a play's timeout (WSL2: nested virtualization in .wslconfig; else the kvm group)")]


def vm(*args, timeout=600):
    return subprocess.run([sys.executable, os.path.join(SIM, "vmhost.py"), *args],
                          cwd=REPO, capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture(scope="module")
def host():
    """The simulated host, up for the module; a registry that reaches it on this test's port."""
    r = vm("up", "--daemon", "--name", NAME, "--port", str(PORT))
    assert r.returncode == 0, r.stdout + r.stderr
    yield NAME
    vm("down", "--name", NAME)


def play(playbook, layout="site.example/layouts/zsim-vm", *args, timeout=900):
    env = {**os.environ, "ANSIBLE_FORCE_COLOR": "0", "PATH": VENV + os.pathsep + os.environ["PATH"]}
    # the registry's vmhost1 answers on port 2222; this test's host on PORT, so as not to
    # collide with one a person has up
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", layout,
                        os.path.join("collections/ansible_collections/rxrbench/core/playbooks", playbook),
                        "-e", f"ansible_port={PORT}", *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout + r.stderr


def test_the_host_is_installed_set_up_and_the_bench_runs_in_it(host):
    rc, out = play("install.yml", "site.example/layouts/hosts", "--limit", "vmhost1")
    assert rc == 0, out
    rc, out = play("host.yml")
    assert rc == 0, out
    assert "sim_bench" in out and "failed=0" in out, out
    rc, out = play("apply.yml", "site.example/layouts/zsim-vm", "--check", "--diff")
    assert rc == 0 and "failed=0" in out, out
    rc, out = play("apply.yml")
    assert rc == 0 and "failed=0" in out, out
    rc, out = play("test.yml")
    assert rc == 0 and "failed=0" in out, out
    rc, out = play("scenario.yml")
    assert rc == 0 and "failed=0" in out, out
