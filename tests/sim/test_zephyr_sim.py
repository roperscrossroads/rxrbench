"""The simulated Zephyr bench: Zephyr's shell sample on native_sim, of no firmware project's,
under the `bare` family (known by its bytes), behind real brokers, through the tool's plays
(`site.example/layouts/zsim`). Needs both images (`just sim-zephyr-build`); skipped without."""
import json
import os
import re
import signal
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(REPO, "venv", "bin")
SIM = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "sim")
# the example bench's registry: site data, beside its layouts
REGISTRY = os.path.join(REPO, "site.example", "sim", "registry.yaml")
STORE = os.path.join(REPO, ".build", "store", "zephyr")
LAYOUT = "site.example/layouts/zsim"

pytestmark = pytest.mark.skipif(
    not all(os.access(os.path.join(STORE, n, "zephyr.exe"), os.X_OK) for n in ("zephyr-shell", "zephyr-shell-b")),
    reason="the simulated Zephyr bench's images are not built (just sim-zephyr-build)")
# the layout's other boards: two on the zephyr plugin's application (test_zephyr_app_sim.py),
# one on the SMP server (test_mcumgr_sim.py), whose plan runs the SMP module in ~/.venv-mcumgr,
# one on NuttX (test_nuttx_sim.py), and the emulated MCU (test_qemu_sim.py). A plan of the
# layout asks every one of them, so the bench here is all seven, as `just sim-zephyr-up` is.
pytestmark = [pytestmark, pytest.mark.skipif(
    not os.access(os.path.join(STORE, "zapp-1.0", "zephyr.exe"), os.X_OK)
    or not os.access(os.path.join(STORE, "zsmp", "zephyr.exe"), os.X_OK)
    or not os.access(os.path.join(REPO, ".build", "store", "nuttx", "nsh", "nuttx"), os.X_OK)
    or not os.access(os.path.expanduser("~/.venv-mcumgr/bin/python"), os.X_OK),
    reason="the zephyr plugin's application, the SMP server or NuttX is not built (just sim-zapp-build, "
           "just sim-zephyr-build, just sim-nuttx-build), or ~/.venv-mcumgr is missing (just hosts-install)")]


def bench_missing():
    """What the seven-board bench still needs, as reasons; empty when it can start."""
    import shutil  # pylint: disable=import-outside-toplevel
    need = {"zephyr-shell, zephyr-shell-b (just sim-zephyr-build)": ("zephyr-shell", "zephyr-shell-b"),
            "zapp-1.0, 1.1, 2.0 (just sim-zapp-build)": ("zapp-1.0", "zapp-1.1", "zapp-2.0"),
            "zsmp, zsmp-b (just sim-zephyr-build)": ("zsmp", "zsmp-b"),
            "zq, zq-b (just sim-qemu-build)": ("zq", "zq-b")}
    out = [why for why, names in need.items()
           if not all(os.path.isfile(os.path.join(STORE, n, "manifest.json")) for n in names)]
    if not os.access(os.path.join(REPO, ".build", "store", "nuttx", "nsh", "nuttx"), os.X_OK):
        out.append("nsh (just sim-nuttx-build)")
    if not shutil.which("qemu-system-arm"):
        out.append("qemu-system-arm")
    if not os.access(os.path.expanduser("~/.venv-mcumgr/bin/python"), os.X_OK):
        out.append("~/.venv-mcumgr (just hosts-install)")
    return out


@pytest.fixture
def bench(tmp_path):
    """`just sim-zephyr-up`, in a directory of its own: the seven boards of the layout."""
    missing = bench_missing()
    if missing:
        pytest.skip("the simulated bench needs: " + "; ".join(missing))
    run = str(tmp_path / "run")
    out = open(tmp_path / "simbench.out", "wb")  # pylint: disable=consider-using-with
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(SIM, "simbench.py"),
                             "--registry", REGISTRY,
                             "--run-dir", run, "zb1", "zb2", "zb3", "zb4", "zb5", "nb1", "zq1"],
                            stdout=out, stderr=subprocess.STDOUT)
    end = time.monotonic() + 15
    while not os.path.exists(os.path.join(run, "simbench.pid")):
        assert proc.poll() is None, open(tmp_path / "simbench.out").read()
        assert time.monotonic() < end, "the simulated bench did not come up"
        time.sleep(0.05)
    yield run
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    out.close()


def play(run, playbook, *args):
    env = {**os.environ, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0", "RXRBENCH_SITE": "site.example",
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "ansible-playbook"), "-i", LAYOUT,
                        os.path.join("collections/ansible_collections/rxrbench/core/playbooks", playbook), *args],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=180, check=False)
    return r.returncode, r.stdout + r.stderr


def recap(out):
    """{host: {ok, changed, failed, skipped, ...}} from the play recap."""
    found = {}
    for m in re.finditer(r"^(\S+)\s+: ((?:\w+=\d+\s*)+)$", out, re.M):
        found[m.group(1)] = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", m.group(2))}
    return found


def manifest(name):
    with open(os.path.join(STORE, name, "manifest.json")) as f:
        return json.load(f)


AS_B = ("--limit", "zb1", "-e", json.dumps({"firmware": {"family": "bare", "image": "zephyr-shell-b"}}))


def test_a_plan_of_boards_on_their_image_changes_nothing(bench):
    rc, out = play(bench, "apply.yml", "--check", "--diff")
    assert rc == 0, out
    for name in ("zb1", "zb2"):
        assert recap(out)[name]["changed"] == 0 and recap(out)[name]["failed"] == 0, out


def test_another_image_is_a_flash_a_reboot_and_the_same_console(bench):
    # The plan says what would change: the hash, and nothing is written.
    rc, out = play(bench, "apply.yml", "--check", "--diff", *AS_B)
    assert rc == 0, out
    assert recap(out)["zb1"]["changed"] == 1, out
    assert manifest("zephyr-shell")["hash"][:12] in out and manifest("zephyr-shell-b")["hash"][:12] in out, out
    # The apply writes it: the process restarts on the new executable.
    rc, out = play(bench, "apply.yml", *AS_B)
    assert rc == 0, out
    assert recap(out)["zb1"]["changed"] == 1 and recap(out)["zb1"]["failed"] == 0, out
    with open(os.path.join(bench, "zb1.pid")) as f:
        pid = int(f.read())
    exe = os.readlink(f"/proc/{pid}/exe")
    assert exe.endswith("zb1.exe"), exe
    # And a second apply of the same changes nothing.
    rc, out = play(bench, "apply.yml", *AS_B)
    assert rc == 0, out
    assert recap(out)["zb1"]["changed"] == 0, out


def test_a_scenario_runs_its_steps_on_the_boards_they_name(bench):
    # the two boards that hold settings get theirs first: a scenario judges declared state
    rc, out = play(bench, "apply.yml", "--limit", "zb3,zb4")
    assert rc == 0, out
    rc, out = play(bench, "scenario.yml")
    assert rc == 0, out
    assert "(item=zb1: kernel version)" in out and "(item=zb2: kernel uptime)" in out, out
    runs = os.path.join(REPO, "runs")
    latest = sorted(d for d in os.listdir(runs) if os.path.isdir(os.path.join(runs, d, "scenario")))[-1]
    with open(os.path.join(runs, latest, "scenario", "both-answer", "scenario.json")) as f:
        record = json.load(f)
    assert record["passed"] is True, record
    assert [s["board"] for s in record["steps"]] == ["zb1", "zb2", "zb1", "zb3", "zb4"], record
    # the library test the layout binds (site.example/tests/), run on the boards bound to its roles
    with open(os.path.join(runs, latest, "scenario", "zephyr-shells-answer", "scenario.json")) as f:
        bound = json.load(f)
    assert bound["passed"] is True and bound["bindings"] == {"a": "zb1", "b": "zb3"}, bound
    assert [s["board"] for s in bound["steps"]] == ["zb1", "zb3", "zb3"], bound
    # A step that cannot come true fails its scenario, after its `within`, and the play.
    scenarios = [{"name": "never", "steps": [
        {"board": "zb2", "cmd": "kernel version", "expect": "^Zephyr version"},
        {"board": "zb1", "cmd": "kernel uptime", "expect": "^no such thing", "within": 2},
        {"board": "zb1", "cmd": "kernel version", "expect": "^Zephyr version"}]}]
    rc, out = play(bench, "scenario.yml", "-e", json.dumps({"bench_scenarios": scenarios}))
    assert rc != 0, out
    assert "FAILED - RETRYING" in out and "failed: never" in out, out
    latest = sorted(d for d in os.listdir(runs) if os.path.isdir(os.path.join(runs, d, "scenario")))[-1]
    with open(os.path.join(runs, latest, "scenario", "never", "scenario.json")) as f:
        record = json.load(f)
    assert record["passed"] is False and len(record["steps"]) == 2, "the third step never ran"


def test_the_device_tests_run_through_the_brokers(bench):
    # The boards whose device tests read what an apply writes (settings on zb3 and zb4, the
    # NuttX board's files) are applied first; the device tests then run on every board.
    rc, out = play(bench, "apply.yml", "--limit", "zb3,zb4,nb1")
    assert rc == 0, out
    rc, out = play(bench, "test.yml")
    assert rc == 0, out
    for name in ("zb1", "zb2", "zb3", "zb4"):
        assert recap(out)[name]["failed"] == 0, out
    assert "version" in out and "uptime" in out, out
    assert "FAIL" not in out, out
