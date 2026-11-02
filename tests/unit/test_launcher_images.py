"""What a Launcher image is (module_utils/images.py) and how a board's declared images are
resolved to files (the launcher_images module): kinds, families, pins, and what a Launcher
release must carry."""
import hashlib

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import artifact
from ansible_collections.rxrbench.launcher.plugins.module_utils import images
from ansible_collections.rxrbench.launcher.plugins.modules import launcher_images
from tests.support import fake_module
from tests.unit import esptables
from tests.unit.test_launcher_webui import esp_image, merged_image


def test_a_plain_app_is_described_whole():
    app = esp_image(b"\x22" * 700)
    d = images.describe(_file(app))
    assert (d["size"], d["app_offset"], d["app_size"], d["merged"], d["manager"]) == (len(app), 0, len(app), False, False)
    assert d["sha256"] == hashlib.sha256(app).hexdigest()


def test_a_merged_image_of_another_firmware_is_merged_but_no_manager():
    app = esp_image(b"\x23" * 700)
    d = images.describe(_file(merged_image(app)))
    assert d["merged"] is True and d["manager"] is False
    assert (d["app_offset"], d["app_size"]) == (0x10000, len(app))


def test_a_launcher_release_carries_an_app_managers_table():
    app = esp_image(b"\x24" * 700)
    d = images.describe(_file(esptables.flash_with_table(esptables.LAUNCHER_4MB) + app))
    assert d["merged"] is True and d["manager"] is True and d["app_size"] == len(app)


def test_what_is_not_an_image_is_said():
    with pytest.raises(images.ImageError, match="not an ESP-IDF app image, nor a merged image"):
        images.app_part(b"\x00" * 0x11000)
    no_app = [esptables.row(1, 0x02, 0x9000, 0x5000, "nvs"), esptables.row(1, 0x00, 0xE000, 0x2000, "otadata")]
    with pytest.raises(images.ImageError, match="no app partition"):
        images.app_part(esptables.flash_with_table(no_app) + esp_image())
    with pytest.raises(images.ImageError, match="no ESP-IDF app image at 0x10000"):
        images.app_part(esptables.flash_with_table(esptables.LAUNCHER_4MB) + b"\xff" * 0x1000)


def test_a_slot_is_the_app_rounded_up_to_64_kb():
    assert images.slot_bytes(1) == 0x10000
    assert images.slot_bytes(0x10000) == 0x10000
    assert images.slot_bytes(0x10001) == 0x20000


# ---- the module: a board's declared images as files -------------------------------------

def catalog(tmp_path):
    release = esptables.flash_with_table(esptables.LAUNCHER_4MB) + esp_image(b"\x31" * 500)
    app = esp_image(b"\x32" * 900)
    (tmp_path / "Launcher.bin").write_bytes(release)
    (tmp_path / "cam.bin").write_bytes(app)
    (tmp_path / "adir").mkdir()
    return {
        "launcher-cyd": {"family": "launcher", "kind": "manager", "version": "2.9.1",
                         "source": {"type": "path", "path": str(tmp_path / "Launcher.bin")}},
        "cam-stream": {"family": "launcher", "kind": "app",
                       "source": {"type": "path", "path": str(tmp_path / "cam.bin"),
                                  "sha256": hashlib.sha256(app).hexdigest()}},
        "cam-stale": {"family": "launcher", "kind": "app",
                      "source": {"type": "path", "path": str(tmp_path / "cam.bin"), "sha256": "ab" * 32}},
        "plain-as-manager": {"family": "launcher", "kind": "manager",
                             "source": {"type": "path", "path": str(tmp_path / "cam.bin")}},
        "a-directory": {"family": "launcher", "kind": "app",
                        "source": {"type": "path", "path": str(tmp_path / "adir")}},
        "mesh-thing": {"family": "mesh", "source": {"type": "path", "path": str(tmp_path / "cam.bin")}},
    }


def test_resolve_checks_the_catalog_entry_before_the_file(tmp_path):
    cat = catalog(tmp_path)
    r = launcher_images.resolve("launcher-cyd", cat, "manager", None)
    assert r["manager"] is True and r["version"] == "2.9.1" and r["image"] == "launcher-cyd"
    r = launcher_images.resolve("cam-stream", cat, "app", None)
    assert r["manager"] is False and r["version"] is None and r["app_size"] == 900 + 24 + 8 + 1 + 32 - 1 or r["app_size"] > 900
    for name, kind, why in [("nope", "app", "not in the firmware catalog"),
                            ("mesh-thing", "app", "is mesh firmware, not launcher"),
                            ("cam-stream", "manager", "is of kind app, wanted manager"),
                            ("launcher-cyd", "app", "is of kind manager, wanted app"),
                            ("cam-stale", "app", "sha256"),
                            ("a-directory", "app", "its source is a directory"),
                            ("plain-as-manager", "manager", "carries no app manager's table")]:
        with pytest.raises(artifact.ArtifactError, match=why):
            launcher_images.resolve(name, cat, kind, None)


def test_the_module_resolves_the_manager_and_every_app_and_lists_the_files(tmp_path, monkeypatch):
    cat = catalog(tmp_path)
    how, r = fake_module.run(launcher_images, monkeypatch, {
        "manager": "launcher-cyd", "apps": {"camstr": {"image": "cam-stream"}}, "images": cat})
    assert how == "exit" and r["changed"] is False
    assert r["manager"]["version"] == "2.9.1" and set(r["apps"]) == {"camstr"}
    assert r["files"] == [str(tmp_path / "Launcher.bin"), str(tmp_path / "cam.bin")]
    how, r = fake_module.run(launcher_images, monkeypatch, {"apps": {"camstr": {"image": "cam-stream"}}, "images": cat})
    assert how == "exit" and r["manager"] is None and r["files"] == [str(tmp_path / "cam.bin")]


def test_the_module_says_which_declaration_is_wrong(tmp_path, monkeypatch):
    cat = catalog(tmp_path)
    how, r = fake_module.run(launcher_images, monkeypatch, {"apps": {"camstr": {}}, "images": cat})
    assert how == "fail" and r["msg"] == "launcher.apps.camstr names no image"
    how, r = fake_module.run(launcher_images, monkeypatch, {"manager": "plain-as-manager", "images": cat})
    assert how == "fail" and "carries no app manager's table" in r["msg"]
    how, r = fake_module.run(launcher_images, monkeypatch, {"apps": {"c": {"image": "cam-stale"}}, "images": cat})
    assert how == "fail" and "sha256" in r["msg"]


def _file(data, name="img.bin"):
    import tempfile
    import os
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(data)
    return p
