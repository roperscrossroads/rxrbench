"""The pinned single-file tools: a download is used only when its sha256 is the pinned one,
an archive gives only the files named, a bare executable lands as itself; and the table the
default install reads is the one pins.py reads."""
import hashlib
import importlib.util
import io
import os
import tarfile

import pytest
import yaml

spec = importlib.util.spec_from_file_location(
    "pins", os.path.join(os.path.dirname(__file__), "..", "..", "tools", "pins.py"))
pins = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pins)


def sha(b):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def release(tmp_path, monkeypatch):
    rel = tmp_path / "rel"
    rel.mkdir()
    raw = b"#!/bin/sh\necho tool\n"
    (rel / "tool-1.0.linux.amd64").write_bytes(raw)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("pkg/one", b"1"), ("pkg/two", b"2"), ("pkg/../../evil", b"x"),
                           ("pkg/LICENSE", b"l")):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    (rel / "pkg-2.0-x86_64.tar.gz").write_bytes(buf.getvalue())
    table = {
        "tool": {"version": "1.0", "url": f"file://{rel}/tool-{{version}}.linux.{{arch}}",
                 "arch": {"x86_64": "amd64"}, "format": "raw",
                 "sha256": {"x86_64": sha(raw)}, "files": ["tool"]},
        "pkg": {"version": "2.0", "url": f"file://{rel}/pkg-{{version}}-{{arch}}.tar.gz",
                "sha256": {"x86_64": sha(buf.getvalue())}, "files": ["one", "two"]},
    }
    monkeypatch.setattr(pins, "ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(pins, "table", lambda: table)
    monkeypatch.setattr(pins, "arch", lambda: "x86_64")
    return tmp_path, table


def install(monkeypatch, *names):
    monkeypatch.setattr("sys.argv", ["pins.py", "install", *names])
    pins.main()


def test_a_bare_executable_and_an_archive_land_in_bin(release, monkeypatch):
    tmp, _ = release
    install(monkeypatch)
    b = tmp / "root" / "bin"
    assert (b / "tool").read_bytes().startswith(b"#!/bin/sh") and os.access(b / "tool", os.X_OK)
    assert (b / "one").read_bytes() == b"1" and (b / "two").read_bytes() == b"2"
    assert not (b / "LICENSE").exists(), "only the files the pin names"
    assert not (tmp / "evil").exists() and not (tmp / "root" / "evil").exists()
    assert os.path.islink(b / "tool")


def test_a_download_with_another_checksum_is_refused_and_removed(release, monkeypatch):
    tmp, table = release
    table["tool"]["sha256"]["x86_64"] = "0" * 64
    with pytest.raises(SystemExit, match="is not the pinned"):
        install(monkeypatch, "tool")
    assert not list((tmp / "root" / "cache").iterdir())
    assert not (tmp / "root" / "bin" / "tool").exists()


def test_an_unknown_name_is_refused(release, monkeypatch):
    with pytest.raises(SystemExit, match="not pinned"):
        install(monkeypatch, "nope")


def test_every_pin_has_both_architectures_and_its_files():
    with open(pins.TABLE) as fh:
        table = yaml.safe_load(fh)
    assert {"uv", "probe-rs", "sops", "age"} <= set(table)
    for name, pin in table.items():
        assert set(pin["sha256"]) == {"aarch64", "x86_64"}, name
        assert all(len(v) == 64 for v in pin["sha256"].values()), name
        assert pin["files"] and "{version}" in pin["url"], name
