"""tools/zapp_build.py and zapp's hardware files: every board row names files that exist, a build
line has the confs in the order Kconfig needs them, the modules a workspace lacks are found by
name, and what a build provides reaches the staged manifest and the catalog entry."""
import json
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "tools"))
import zapp_build  # noqa: E402  pylint: disable=wrong-import-position

KNOWN_MODULES = {"hal_rpi_pico", "hal_infineon"}


def test_every_board_row_names_its_target_type_and_files_that_exist():
    for name, row in zapp_build.boards().items():
        assert row.get("target") and row.get("board_tag"), name
        for f in (row.get("confs") or []) + (row.get("overlays") or []):
            assert os.path.isfile(os.path.join(zapp_build.HW, f)), f"{name}: {f}"
        assert set(row.get("modules") or []) <= KNOWN_MODULES, name


def test_every_option_the_hardware_confs_set_for_zapp_is_one_its_kconfig_declares():
    with open(os.path.join(zapp_build.ZAPP, "Kconfig")) as f:
        declared = set(re.findall(r"^config (ZAPP_\w+)", f.read(), re.M))
    for conf in os.listdir(zapp_build.HW):
        if conf.endswith(".conf"):
            with open(os.path.join(zapp_build.HW, conf)) as f:
                used = set(re.findall(r"^CONFIG_(ZAPP_\w+)=", f.read(), re.M))
            assert used <= declared, f"{conf}: {used - declared}"


def test_each_feature_the_build_lists_has_its_source_file():
    with open(os.path.join(zapp_build.ZAPP, "CMakeLists.txt")) as f:
        names = re.search(r"foreach\(f ([A-Z ]+)\)", f.read()).group(1).split()
    for n in names:
        assert os.path.isfile(os.path.join(zapp_build.ZAPP, "src", n.lower() + ".c")), n


def test_a_build_line_puts_the_hardware_conf_first_and_the_version_last():
    row = zapp_build.boards()["kb2040"]
    cmd = zapp_build.command("/ws/.venv/bin/west", row, "3.1", "/b", ["/m/hal_rpi_pico"])
    assert cmd[:7] == ["/ws/.venv/bin/west", "build", "-p", "always", "-b", "adafruit_kb2040", "-S"]
    confs = next(c for c in cmd if c.startswith("-DEXTRA_CONF_FILE=")).split("=", 1)[1].split(";")
    assert confs[0].endswith("hw/hw.conf") and confs[-1].endswith("zephyr/zapp-3.1.conf")
    assert any(c.endswith("hw/locate.conf") for c in confs)
    assert "-DZEPHYR_EXTRA_MODULES=/m/hal_rpi_pico" in cmd
    assert any(c.startswith("-DEXTRA_DTC_OVERLAY_FILE=") and "storage-rp2040-8m.overlay" in c for c in cmd)


def test_a_module_the_workspace_holds_is_left_to_west_and_another_is_found_by_its_directory(tmp_path):
    (tmp_path / "ws" / "modules" / "hal" / "rpi_pico").mkdir(parents=True)
    extra = [f"{tmp_path}/x/hal_infineon", f"{tmp_path}/y/other"]
    assert zapp_build.module_dirs(str(tmp_path / "ws"), ["hal_rpi_pico", "hal_infineon"], extra) == \
        [f"{tmp_path}/x/hal_infineon"]
    with pytest.raises(SystemExit, match="--module DIR"):
        zapp_build.module_dirs(str(tmp_path / "ws"), ["hal_infineon"], [])


def test_the_catalog_entry_carries_the_features_the_build_listed():
    row = zapp_build.boards()["pico2w"]
    e = zapp_build.catalog_entry("zapp-3.0-pico2w", row, "3.0", ["digest", "wifi"])["zapp-3.0-pico2w"]
    assert e["features"] == ["digest", "wifi"] and e["family"] == "zephyr_settings"
    assert e["board_tags"] == ["raspberrypi-pico2w"] and e["flasher"] == "picoboot"


def test_a_staged_image_carries_its_features_in_its_manifest(tmp_path):
    img = tmp_path / "app.bin"
    img.write_bytes(b"\x00" * 256)
    r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "stage_raw.py"), str(img), "--name", "x",
                        "--board-tag", "t", "--store", str(tmp_path / "store"), "--features", "wifi,digest"],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    with open(tmp_path / "store" / "bare" / "x" / "manifest.json") as f:
        assert json.load(f)["features"] == ["digest", "wifi"]
