"""tools/stage_catalog.py: a catalogued image from its published source into the image store."""
import importlib.util
import json
import os
import struct
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("stage_catalog", os.path.join(REPO, "tools", "stage_catalog.py"))
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

ENTRY = {"family": "fw", "board_tags": ["kit-a"], "version": "2.7.26.54e0d8d",
         "source": {"type": "github", "repo": "o/r", "tag": "v1", "asset": "fw.zip",
                    "sha256": "ab" * 32, "member": "fw-kit-a.uf2"}}


def test_a_release_version_names_its_build():
    assert sc.build_of(ENTRY) == "54e0d8d"
    assert sc.build_of({**ENTRY, "build": "deadbeef"}) == "deadbeef", "the entry's own wins"
    assert sc.build_of({"version": "1.2.3"}) is None


def test_what_cannot_be_staged_here_is_refused_with_why():
    with pytest.raises(SystemExit, match="not in the firmware catalogs"):
        sc.plan("nope", {})
    with pytest.raises(SystemExit, match="expected at .* and not there: stage it there"):
        sc.plan("p", {"p": {**ENTRY, "source": {"type": "path", "path": "no/such/dir"}}})
    two = {"t": {**ENTRY, "board_tags": ["kit-a", "kit-b"]}}
    with pytest.raises(SystemExit, match="name one with --board-tag"):
        sc.plan("t", two)
    assert sc.plan("t", two, "kit-b")[1] == "kit-b"
    with pytest.raises(SystemExit, match="not kit-c"):
        sc.plan("t", two, "kit-c")


def _uf2(path):
    blk = bytearray(512)
    struct.pack_into("<8I", blk, 0, 0x0A324655, 0x9E5D5157, 0x2000, 0x27000, 256, 0, 1, 0xADA52840)
    struct.pack_into("<I", blk, 508, 0x0AB16F30)
    path.write_bytes(bytes(blk))


def test_a_fetched_file_is_staged_with_the_version_and_build_it_reports(tmp_path, monkeypatch):
    uf2 = tmp_path / "fw-kit-a.uf2"
    _uf2(uf2)
    monkeypatch.setattr(sc.layout, "load_catalog", lambda *d: {"images": {"fw-2.7": ENTRY}})
    monkeypatch.setattr(sc.artifact, "fetch", lambda src: str(uf2))
    ran, real = [], subprocess.run
    monkeypatch.setattr(sc.subprocess, "run", lambda argv, check: ran.append(argv) or
                        real(argv, check=check))
    monkeypatch.setattr(sys, "argv", ["stage_catalog.py", "fw-2.7", "--store", str(tmp_path / "store")])
    with pytest.raises(SystemExit) as e:
        sc.main()
    assert e.value.code == 0
    m = json.loads((tmp_path / "store" / "fw" / "fw-2.7" / "manifest.json").read_text())
    assert (m["version"], m["build"], m["board_tag"], m["format"]) == ("2.7.26.54e0d8d", "54e0d8d", "kit-a", "uf2")


def test_a_path_source_staged_already_is_done(tmp_path, capsys):
    """Nothing to fetch is not a failure: exit 0, naming where it is."""
    (tmp_path / "img").mkdir()
    with pytest.raises(SystemExit) as e:
        sc.plan("p", {"p": {**ENTRY, "source": {"type": "path", "path": str(tmp_path / "img")}}})
    assert e.value.code == 0
    assert "staged already" in capsys.readouterr().out



def _zip(tmp_path, files):
    import zipfile
    z = tmp_path / "rel.zip"
    with zipfile.ZipFile(z, "w") as w:
        for n, b in files.items():
            w.writestr(n, b)
    return z


def _entry(**kw):
    return {**ENTRY, "flasher": "esp-image",
            "source": {**ENTRY["source"], "member": "fw-kit-a.factory.bin"}, **kw}


class _Plugin:
    """A family that says its release writes `extra.bin` into ota_1, with the md5 it gives."""
    def __init__(self, md5):
        self.md5, self.asked = md5, []

    def release_partitions(self, member, read):
        self.asked.append((member, read("extra.bin"), read("absent.bin")))
        return [{"partition": "ota_1", "member": "extra.bin", "md5": self.md5}]


def _wire(tmp_path, monkeypatch, plugin):
    from ansible_collections.rxrbench.core.plugins.module_utils import artifact
    z = _zip(tmp_path, {"fw-kit-a.factory.bin": b"F", "extra.bin": b"E", "own.bin": b"O"})
    monkeypatch.setattr(sc.artifact, "fetch", lambda src: artifact._member(
        str(z), src["member"], str(tmp_path / "c")) if src.get("member") else str(z))
    monkeypatch.setattr(sc.families, "plugin", lambda family: plugin)


def test_a_release_is_staged_with_what_its_family_says_its_full_install_writes(tmp_path, monkeypatch):
    import hashlib
    plugin = _Plugin(hashlib.md5(b"E").hexdigest())
    _wire(tmp_path, monkeypatch, plugin)
    got = [(label, os.path.basename(p)) for label, p in sc.partition_images("x", _entry())]
    assert got == [("ota_1", "extra.bin")]
    assert plugin.asked == [("fw-kit-a.factory.bin", b"E", None)], "the plugin reads the zip"


def test_a_file_whose_md5_is_not_the_release_s_is_refused(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, _Plugin("0" * 32))
    with pytest.raises(SystemExit, match="extra.bin has md5 .* the release says 0+"):
        sc.partition_images("x", _entry())


def test_the_entry_s_own_partition_images_win(tmp_path, monkeypatch):
    plugin = _Plugin("0" * 32)
    _wire(tmp_path, monkeypatch, plugin)
    got = sc.partition_images("x", _entry(partition_images={"spiffs": "own.bin"}))
    assert [(label, os.path.basename(p)) for label, p in got] == [("spiffs", "own.bin")]
    assert plugin.asked == [], "the plugin is not asked"
    assert sc.partition_images("x", _entry(flasher="uf2")) == []
