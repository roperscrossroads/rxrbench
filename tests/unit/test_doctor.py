"""tools/doctor.py: what a machine has of what the simulated benches need. On a machine
with nothing it names every prerequisite and refuses the benches asked for; it never
requires an image, which the recipes build."""
import importlib.util
import json
import os
import subprocess
import sys

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
DOCTOR = os.path.join(REPO, "tools", "doctor.py")
SPEC = importlib.util.spec_from_file_location("doctor", DOCTOR)
doctor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doctor)

BENCHES = ("workstation", "containers", "zephyr", "qemu", "nuttx", "smp", "vmhost", "ab", "secrets")


def bare(tmp_path):
    """An environment with no tools on PATH, no workspaces and an empty home."""
    empty = tmp_path / "bin"
    empty.mkdir()
    return {"PATH": str(empty), "HOME": str(tmp_path / "home")}


def test_a_bare_machine_lacks_every_prerequisite_and_the_exit_code_says_so(tmp_path):
    env = bare(tmp_path)
    r = subprocess.run([sys.executable, DOCTOR, "--json"], env=env, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr          # nothing required: a report, not a refusal
    report = json.loads(r.stdout)
    assert set(report) == set(BENCHES)
    for bench in ("zephyr", "qemu", "nuttx", "smp", "secrets"):
        assert all(not ok for ok, _, _ in report[bench]["need"]), (bench, report[bench]["need"])
    hints = [detail for bench in BENCHES for _ok, _what, detail in report[bench]["need"]]
    assert any("apt install" in h for h in hints) and any("docs/SIMULATED.md" in h for h in hints)
    r = subprocess.run([sys.executable, DOCTOR, "--require", "zephyr,nuttx"], env=env,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 1 and "cannot run nuttx, zephyr" in r.stdout, r.stdout


def test_images_are_reported_but_never_required(tmp_path):
    env = bare(tmp_path)
    r = subprocess.run([sys.executable, DOCTOR, "--json"], env=env, capture_output=True, text=True, check=False)
    report = json.loads(r.stdout)
    assert [what for _ok, what, _d in report["zephyr"]["built"]] == [
        "image zephyr-shell", "image zephyr-shell-b", "image zsmp", "image zsmp-b",
        "image zapp-1.0", "image zapp-1.1", "image zapp-2.0"]
    assert all(not ok for ok, _, _ in report["ab"]["built"])


def test_an_unknown_bench_is_refused(tmp_path):
    r = subprocess.run([sys.executable, DOCTOR, "--require", "mars"], env=bare(tmp_path),
                       capture_output=True, text=True, check=False)
    assert r.returncode != 0 and "unknown bench mars" in r.stderr


def test_a_version_is_read_from_a_commands_output(tmp_path):
    fake = tmp_path / "cmake"
    fake.write_text("#!/bin/sh\necho 'cmake version 3.31.6'\n")
    fake.chmod(0o755)
    assert doctor.version_of([str(fake), "--version"]) == (3, 31, 6)
    assert doctor.version_of([str(tmp_path / "absent"), "--version"]) is None
