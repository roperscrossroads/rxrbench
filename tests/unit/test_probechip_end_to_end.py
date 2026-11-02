"""A plugin brings a probe-flashed chip family end to end (docs/ARCHITECTURE.md#core-portability):
the fixture plugin's `probechip`, a chip with no console and no partition table behind a
debug probe, on a site of its own (its plugin under `<site>/collections`, its image in the
site's catalog), from an image built elsewhere (an Intel HEX file linked behind a bootloader).

The layout picks the plugin's flasher on the probe route and its update step; the plan shows
the write, the apply puts the bytes where the image is linked, the plan after it is clean and
its state record names what the chip runs; the device tests run through the family's step
tasks; the chip is backed up, a config range and the whole chip erased (`erase-chip`), and
written back through the plugin's task files."""
import json
import os
import subprocess
import sys

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOKS = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "playbooks")
FIXTURES = os.path.join(REPO, "tests", "fixtures", "collections")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_imagefile import ihex  # noqa: E402  pylint: disable=wrong-import-position

FLASH = 1 << 20
APP = bytes(range(256)) * 4
AT = 0x4000     # 0x08004000, behind a 16 KiB bootloader
CONFIG = 0xF8000  # 0x080F8000, the config range the family declares


@pytest.fixture
def bench(tmp_path):
    site = tmp_path / "site"
    layout = site / "layouts" / "probe"
    (layout / "group_vars").mkdir(parents=True)
    (layout / "host_vars").mkdir()
    (site / "catalog").mkdir()
    os.symlink(FIXTURES, site / "collections")
    (site / "plugins.yml").write_text("plugins: [fixture.widgets]\n")
    (site / "registry.yaml").write_text(yaml.safe_dump({"version": 1, "hosts": {"work": {"local": True}},
        "protected": [], "devices": {
            "probe1": {"kind": "usb", "match": {"usb_vid": "0483", "usb_pid": "3748"},
                       "reach": {"usb": {"host": "work"}}},
            "p1": {"kind": "swd", "reach": {"swd": {
                "host": "work", "probe": "probe1", "chip": "STM32F103C8",
                "uid": "0123456789abcdef01234567", "state": "live", "can": ["flash", "diag"]}}}}}))
    store = tmp_path / "store" / "probechip" / "probe-app"
    (site / "catalog" / "firmware.yml").write_text(yaml.safe_dump({"images": {"probe-app": {
        "family": "probechip", "summary": "built elsewhere, linked behind a bootloader",
        "flasher": "probeflash", "source": {"type": "path", "path": str(store)}, "rf": "none"}}}))
    (layout / "devreg.yml").write_text(
        "plugin: rxrbench.core.devreg\nregistry: ../../registry.yaml\nboards:\n  p1: {host: work}\n")
    (layout / "group_vars" / "all.yml").write_text(yaml.safe_dump({
        "bench_apply_allowed": True,
        "ansible_python_interpreter": os.path.join(REPO, "venv", "bin", "python"),
        "bench_image": str(store), "probechip_flash": str(tmp_path / "chip.bin"),
        "bench_tests": [{"name": "version", "cmd": "widget version", "expect": "^widget "}]}))
    (layout / "host_vars" / "p1.yml").write_text("firmware: {family: probechip, image: probe-app}\n")
    (tmp_path / "app.hex").write_text(ihex([(0x08000000 + AT, APP)]))
    (tmp_path / "chip.bin").write_bytes(b"\xff" * FLASH)
    (tmp_path / "loc").mkdir()      # no location index: the first read says whether it is there
    env = dict(os.environ, RXRBENCH_SITE=str(site), RXRBENCH_LOCATION_DIR=str(tmp_path / "loc"),
               ANSIBLE_COLLECTIONS_PATH=os.pathsep.join([os.path.join(REPO, "collections"),
                                                         str(site / "collections")]),
               ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0")
    where = ["-e", json.dumps({"bench_repo": str(tmp_path), "bench_runs_dir": str(tmp_path / "runs"),
                               "board_host_store": str(tmp_path / "host-store"),
                               "bench_facts_dir": str(tmp_path / "facts")})]

    def play(playbook, *args, callbacks=None, xdg=None):
        e = dict(env, ANSIBLE_CALLBACKS_ENABLED=callbacks) if callbacks else dict(env)
        if xdg:
            e["XDG_RUNTIME_DIR"] = xdg
        r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-playbook"), "-i", str(layout),
                            os.path.join(PLAYBOOKS, playbook), *where, *args],
                           cwd=REPO, env=e, capture_output=True, text=True, timeout=240)
        return r.returncode, " ".join((r.stdout + r.stderr).split())

    def stage():
        r = subprocess.run([os.path.join(REPO, "venv", "bin", "python"), os.path.join(REPO, "tools", "stage_raw.py"),
                            str(tmp_path / "app.hex"), "--name", "probe-app", "--family", "probechip",
                            "--board-tag", "none", "--store", str(tmp_path / "store")],
                           capture_output=True, text=True, check=False)
        assert r.returncode == 0, r.stdout + r.stderr
    play.stage, play.chip, play.tmp = stage, tmp_path / "chip.bin", tmp_path
    return play


def recap(out):
    return out[out.index("PLAY RECAP"):]


def test_a_probe_flashed_family_from_an_image_built_elsewhere_end_to_end(bench):
    bench.stage()
    manifest = json.loads((bench.tmp / "store" / "probechip" / "probe-app" / "manifest.json").read_text())
    assert manifest["base"] == "0x8004000" and manifest["converted_from"]["format"] == "ihex"

    rc, out = bench("apply.yml", "--check", "--diff")
    assert rc == 0 and "changed=1" in recap(out), out
    assert bench.chip.read_bytes() == b"\xff" * FLASH, "a plan writes nothing"

    rc, out = bench("apply.yml")
    assert rc == 0 and "changed=1" in recap(out), out
    flash = bench.chip.read_bytes()
    assert flash[AT:AT + len(APP)] == APP, "written where the image is linked"
    assert flash[:AT] == b"\xff" * AT and flash[AT + len(APP):] == b"\xff" * (FLASH - AT - len(APP))

    rc, out = bench("apply.yml", "--check", "-e", "bench_run=r1", callbacks="rxrbench.core.state")
    assert rc == 0 and "changed=0" in recap(out), out
    state = json.loads((bench.tmp / "runs" / "r1" / "state.json").read_text())
    p1 = state["boards"]["p1"]
    assert state["clean"] is True and p1["reported"]["hash"] == manifest["hash"], state

    rc, out = bench("test.yml")
    assert rc == 0 and "failed=0" in recap(out), out

    config = bytes(range(256)) * 128              # what the firmware keeps in its config range
    flash = flash[:CONFIG] + config + flash[CONFIG + len(config):]
    bench.chip.write_bytes(flash)
    rc, out = bench("backup.yml")
    assert rc == 0 and "p1: regions" in out and "(config)" in out and "chip p1 (1MB)" in out, out

    bench.chip.write_bytes(flash[:CONFIG] + b"\x00" * len(config) + flash[CONFIG + len(config):])
    rc, out = bench("restore-regions.yml", "--check")
    assert rc == 0 and "changed=1" in recap(out), out
    rc, out = bench("restore-regions.yml")
    assert rc == 0 and "changed=1" in recap(out) and "config" in out, out
    assert bench.chip.read_bytes() == flash, "the config range back, nothing else touched"

    rc, out = bench("erase.yml", "-e", "erase_region=config")
    assert rc == 0 and "config erased" in out, out
    assert bench.chip.read_bytes() == flash[:CONFIG] + b"\xff" * len(config) + flash[CONFIG + len(config):]
    rc, out = bench("erase.yml", "-e", "erase_region=app")
    assert rc != 0 and "not one of this firmware's config regions" in out, out
    rc, out = bench("restore-regions.yml")
    assert rc == 0 and bench.chip.read_bytes() == flash

    rc, out = bench("erase.yml")
    assert rc != 0 and "name the config region to erase" in out and "erase-chip" in out, out
    assert bench.chip.read_bytes() == flash, "no region named: nothing erased"
    rc, out = bench("erase-chip.yml")
    assert rc == 0 and "erased through the probe" in out, out
    assert bench.chip.read_bytes() == b"\xff" * FLASH
    rc, out = bench("apply.yml", "--check")
    assert rc == 0 and "changed=1" in recap(out), "an erased chip no longer runs its image"
    rc, out = bench("restore-chip.yml", "-e", f"bench_repo={REPO}")
    assert rc == 0 and "written back through the probe" in out, out
    assert bench.chip.read_bytes() == flash
    rc, out = bench("apply.yml", "--check")
    assert rc == 0 and "changed=0" in recap(out), out


def test_an_image_linked_outside_the_chip_is_refused_before_anything_is_written(bench):
    (bench.tmp / "app.hex").write_text(ihex([(0x08000000 + FLASH - 16, APP)]))
    bench.stage()
    rc, out = bench("apply.yml")
    assert rc != 0 and "outside the chip's" in out, out
    assert bench.chip.read_bytes() == b"\xff" * FLASH


def test_a_run_in_which_no_board_answered_fails_and_says_so(bench):
    """The one board of the layout is not there (its probe sees no chip): it is skipped as a
    board that does not answer, and since no board answered, the plan and the apply fail
    instead of reading as a clean run."""
    bench.stage()
    bench.chip.unlink()
    for args in (("--check", "--diff"), ()):
        rc, out = bench("apply.yml", *args)
        assert rc != 0 and "the probe sees no chip" in out and "skipped" in out, out
        assert "no board answered" in out and "p1 skipped" in out, out


def test_an_image_linked_over_the_bootloader_is_refused_unless_the_layout_names_it(bench):
    """The probe chip declares its ranges (a 16 KiB bootloader first): an image linked at the
    start of flash would replace the bootloader, so the plan and the apply refuse it, until
    bench_overwrite names the range (docs/ARCHITECTURE.md#partitions-and-the-write-rule)."""
    (bench.tmp / "app.hex").write_text(ihex([(0x08000000, APP)]))
    bench.stage()
    for args in (("--check", "--diff"), ()):
        rc, out = bench("apply.yml", *args)
        assert rc != 0 and "covers bootloader at 0x8000000" in out and "bench_overwrite" in out, out
    assert bench.chip.read_bytes() == b"\xff" * FLASH, "nothing written"
    rc, out = bench("apply.yml", "-e", json.dumps({"bench_overwrite": ["bootloader"]}))
    assert rc == 0 and "changed=1" in recap(out), out
    assert bench.chip.read_bytes()[:len(APP)] == APP


def test_a_board_declared_a_console_family_with_no_broker_stops_before_anything(bench, tmp_path):
    """A layout that moves a board to a family with a console needs its broker, which only
    hosts-setup starts: the apply stops at once and says so, rather than writing the image and
    failing at the config step after it (the plan stops the same way)."""
    site, layout = tmp_path / "site", tmp_path / "site" / "layouts" / "probe"
    reg = yaml.safe_load((site / "registry.yaml").read_text())
    reg["devices"]["w1"] = {"kind": "usb", "match": {"usb_vid": "2fe3", "usb_pid": "0100",
                                                     "serial": "W1"},
                            "reach": {"usb": {"host": "work"}}}
    (site / "registry.yaml").write_text(yaml.safe_dump(reg))
    (layout / "devreg.yml").write_text("plugin: rxrbench.core.devreg\nregistry: ../../registry.yaml\n"
                                       "boards:\n  p1: {host: work}\n  w1: {host: work}\n")
    cat = yaml.safe_load((site / "catalog" / "firmware.yml").read_text())
    cat["images"]["widget-app"] = {"family": "widget", "summary": "a widget's image",
                                   "source": {"type": "path", "path": str(tmp_path / "w")}}
    (site / "catalog" / "firmware.yml").write_text(yaml.safe_dump(cat))
    (layout / "host_vars" / "w1.yml").write_text("firmware: {family: widget, image: widget-app}\n")
    run = tmp_path / "xdg"
    (run / "rxrbench").mkdir(parents=True)
    for check in ([], ["--check"]):
        rc, out = bench("apply.yml", "--limit", "w1", *check,
                        xdg=str(run))
        assert rc != 0 and "no serial broker runs for it" in out and "just hosts-setup probe" in out, out
        assert "Nothing was read or written" in out
    assert bench.chip.read_bytes() == b"\xff" * FLASH
