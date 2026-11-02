import functools
import hashlib
import http.server
import os
import threading
import zipfile

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import artifact

BODY = b"firmware bytes " * 1000
PIN = hashlib.sha256(BODY).hexdigest()


@pytest.fixture
def server(tmp_path):
    """A local HTTP server over a directory, counting requests and seeing their headers."""
    root = tmp_path / "www"
    root.mkdir()
    seen = []

    class H(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            super().do_GET()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def test_where_each_source_type_points():
    assert artifact.locate({"type": "path", "path": "/x/y.bin"}) == ("path", "/x/y.bin")
    assert artifact.locate({"type": "github", "repo": "o/r", "tag": "v1", "asset": "a.zip"}) == \
        ("url", "https://github.com/o/r/releases/download/v1/a.zip")
    assert artifact.locate({"type": "forgejo", "base": "https://git.example/", "repo": "o/r",
                            "tag": "v1", "asset": "a.bin"}) == \
        ("url", "https://git.example/o/r/releases/download/v1/a.bin")
    assert artifact.locate({"type": "forgejo", "base": "https://git.example", "owner": "o",
                            "package": "fw", "version": "0.4.38", "file": "c4.bin"}) == \
        ("url", "https://git.example/api/packages/o/generic/fw/0.4.38/c4.bin")


def test_a_malformed_source_says_what_is_missing():
    assert "repo" in artifact.check_source({"type": "github", "tag": "v1"})[0]
    assert "unknown source type" in artifact.check_source({"type": "ftp"})[0]
    assert "unknown source type" in artifact.check_source(None)[0]


def test_a_remote_source_must_be_pinned():
    (p,) = artifact.check_source({"type": "url", "url": "https://x/y.bin"})
    assert "pinned" in p
    assert "64 lowercase hex" in artifact.check_source({"type": "url", "url": "https://x/y", "sha256": "ABC"})[0]
    assert artifact.check_source({"type": "path", "path": "/x"}) == [], "a declaration alone is not looked for"


def test_a_path_source_is_looked_for_only_when_asked(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(BODY)
    src = {"type": "path", "path": str(f), "sha256": PIN}
    assert artifact.check_source(src) == [] and artifact.check_source(src, on_disk=True) == []
    (p,) = artifact.check_source({"type": "path", "path": str(tmp_path / "gone.bin")}, on_disk=True)
    assert "no such path" in p
    (p,) = artifact.check_source({**src, "sha256": "ab" * 32}, on_disk=True)
    assert "the pin says" in p
    d = tmp_path / "parts"
    d.mkdir()
    assert artifact.check_source({"type": "path", "path": str(d), "sha256": PIN}, on_disk=True) == [], \
        "a pinned directory is not hashed"


def test_a_download_is_verified_cached_and_not_fetched_twice(server, tmp_path):
    root, base, seen = server
    (root / "fw.bin").write_bytes(BODY)
    src = {"type": "url", "url": f"{base}/fw.bin", "sha256": PIN}
    cache = str(tmp_path / "cache")
    got = artifact.fetch(src, cache)
    assert open(got, "rb").read() == BODY and PIN in got
    assert artifact.fetch(src, cache) == got
    assert len(seen) == 1


def test_a_download_that_does_not_match_its_pin_is_refused_and_not_kept(server, tmp_path):
    root, base, seen = server
    (root / "fw.bin").write_bytes(b"something else")
    cache = tmp_path / "cache"
    with pytest.raises(artifact.ArtifactError, match="the pin says"):
        artifact.fetch({"type": "url", "url": f"{base}/fw.bin", "sha256": PIN}, str(cache))
    assert [p for p in cache.rglob("*") if p.is_file()] == []


def test_a_token_is_sent_from_the_environment(server, tmp_path, monkeypatch):
    root, base, seen = server
    (root / "fw.bin").write_bytes(BODY)
    monkeypatch.setenv("MB_TEST_TOKEN", "s3cret")
    artifact.fetch({"type": "url", "url": f"{base}/fw.bin", "sha256": PIN,
                    "token_env": "MB_TEST_TOKEN"}, str(tmp_path / "cache"))
    assert seen[0][1] == "token s3cret"


def test_one_member_of_a_zip(server, tmp_path):
    root, base, seen = server
    with zipfile.ZipFile(root / "release.zip", "w") as z:
        z.writestr("firmware-heltec-v4.factory.bin", BODY)
        z.writestr("other.bin", b"x")
    pin = artifact.sha256_file(root / "release.zip")
    src = {"type": "url", "url": f"{base}/release.zip", "sha256": pin,
           "member": "firmware-heltec-v4.factory.bin"}
    got = artifact.fetch(src, str(tmp_path / "cache"))
    assert open(got, "rb").read() == BODY
    with pytest.raises(artifact.ArtifactError, match="no member"):
        artifact.fetch({**src, "member": "nope.bin"}, str(tmp_path / "cache"))


def test_a_local_path_is_used_in_place_and_checked_against_its_pin(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(BODY)
    assert artifact.fetch({"type": "path", "path": str(f)}) == str(f)
    assert artifact.fetch({"type": "path", "path": str(f), "sha256": PIN}) == str(f)
    with pytest.raises(artifact.ArtifactError, match="the pin says"):
        artifact.fetch({"type": "path", "path": str(f), "sha256": "0" * 64})
    with pytest.raises(artifact.ArtifactError, match="no such path"):
        artifact.fetch({"type": "path", "path": str(tmp_path / "missing")})


def test_parts_of_a_directory_image_and_of_a_single_file(tmp_path):
    for n in ("bootloader.bin", "partitions.bin", "firmware.bin"):
        (tmp_path / n).write_bytes(b"x")
    img = {"parts": [{"offset": 0x10000, "file": "firmware.bin"}, {"offset": 0, "file": "bootloader.bin"},
                     {"offset": 0x8000, "file": "partitions.bin"}]}
    got = artifact.parts(img, str(tmp_path))
    assert [p["offset"] for p in got] == [0, 0x8000, 0x10000]
    assert artifact.parts({}, str(tmp_path / "firmware.bin")) == \
        [{"offset": 0, "path": str(tmp_path / "firmware.bin")}]
    with pytest.raises(artifact.ArtifactError, match="missing part"):
        artifact.parts({"parts": [{"offset": 0, "file": "nope.bin"}]}, str(tmp_path))
    with pytest.raises(artifact.ArtifactError, match="must name its parts"):
        artifact.parts({}, str(tmp_path))
