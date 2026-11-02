"""The Launcher plugin's WebUI client and launcherctl, against a fake WebUI that answers the
way Launcher's own handlers do: a login that sets one session cookie and clears the others,
401 without it, 302 to "/" for a route the build does not have, multipart uploads whose
"folder" field must come before the file."""
import http.server
import json
import os
import threading
import urllib.parse

import pytest

from ansible_collections.rxrbench.launcher.plugins.module_utils import webui
from ansible_collections.rxrbench.launcher.tools import launcherctl


def esp_image(payload=b"\x11" * 100, hashed=True):
    head = bytearray(24)
    head[0], head[1], head[23] = 0xE9, 1, 1 if hashed else 0
    seg = (0x3C000020).to_bytes(4, "little") + len(payload).to_bytes(4, "little") + payload
    body = bytes(head) + seg
    end = -(-(len(body) + 1) // 16) * 16      # the checksum byte ends a 16-byte block
    if hashed:
        end += 32
    return body + b"\0" * (end - len(body))


def merged_image(app):
    data = bytearray(b"\xff" * 0x10000)
    entries = [(0x01, 0x02, 0x9000, 0x5000, b"nvs"), (0x00, 0x10, 0x10000, 0x200000, b"app0"), (0x01, 0x82, 0xE000, 0x1000, b"spiffs")]
    for i, (t, s, off, size, label) in enumerate(entries):
        e = b"\xaa\x50" + bytes([t, s]) + off.to_bytes(4, "little") + size.to_bytes(4, "little") \
            + label.ljust(16, b"\0") + b"\0" * 4
        data[0x8000 + 32 * i:0x8000 + 32 * (i + 1)] = e
    return bytes(data) + app


class Fake:
    def __init__(self, nvs=True):
        self.password = "pw"
        self.token = None
        self.logins = 0
        self.files = {"/": {"old.bin": 2 * 1024 * 1024}}
        self.entries = [
            {"type": 0, "subtype": 0x20, "subtypeName": "test", "label": "launcher",
             "offset": 0x10000, "size": 0x180000, "running": True},
            {"type": 0, "subtype": 0x10, "subtypeName": "ota_0", "label": "m9base",
             "offset": 0x190000, "size": 0x250000, "running": False, "appName": "m9base",
             "dataLabels": ["spiffs"]},
            {"type": 1, "subtype": 0x82, "subtypeName": "spiffs", "label": "spiffs",
             "offset": 0x3E0000, "size": 0x100000, "running": False},
        ]
        self.free = [{"offset": 0x4E0000, "size": 0xB20000}]
        self.nvs = nvs
        self.booted = None
        self.uploads = []
        self.ota = None
        self.alive = True

    def table(self):
        return {"flashSize": 0x1000000, "dirty": False, "entries": self.entries,
                "freeRanges": self.free}


def handler_for(fake):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, status, body=b"", ctype="text/plain", headers=()):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            for k, v in headers:
                self.send_header(k, v)
            body = body if isinstance(body, bytes) else body.encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authed(self):
            ok = fake.token and f"ESP32SESSION={fake.token}" in (self.headers.get("Cookie") or "")
            if not ok:
                self.send(401, "Unauthorized")
            return ok

        def body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def form(self):
            return dict(urllib.parse.parse_qsl(self.body().decode()))

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(url.query))
            if url.path == "/ping":
                return self.send(200, "launcher-pong") if fake.alive else self.send(302, headers=[("Location", "/")])
            if url.path == "/systeminfo":
                return self.send(200, json.dumps({
                    "VERSION": "2.10.0", "SD": {"totalBytes": 8e9, "usedBytes": 1e9, "freeBytes": 7e9},
                    "APPS": [{"label": "m9base", "name": "m9base"}]}), "application/json")
            if url.path == "/nvs" and not fake.nvs:
                return self.send(302, headers=[("Location", "/")])
            if not self.authed():
                return None
            if url.path == "/partitions":
                return self.send(200, json.dumps(fake.table()), "application/json")
            if url.path == "/nvs":
                return self.send(200, json.dumps({"launcher": [{"k": "a"}, {"k": "b"}],
                                                  "l_apps": [{"k": "n"}]}), "application/json")
            if url.path == "/listfiles":
                folder = q.get("folder", "/")
                lines = [f"pa:{folder}:0"] + [f"Fi:{n}:{webui.human_size(s)}"
                                              for n, s in fake.files.get(folder, {}).items()]
                return self.send(200, "\n".join(lines) + "\n")
            if url.path == "/file":
                folder, _s, name = q["name"].rpartition("/")
                folder = folder or "/"
                if name not in fake.files.get(folder, {}):
                    return self.send(400, "ERROR: file does not exist")
                if q["action"] == "delete":
                    del fake.files[folder][name]
                    return self.send(200, "Deleted : " + q["name"])
                return self.send(200, b"\x01" * 10, "application/octet-stream")
            if url.path == "/reboot":
                fake.alive = False
                return self.send(200, "Rebooting")
            return self.send(302, headers=[("Location", "/")])

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/login":
                f = self.form()
                if f.get("username") == "admin" and f.get("password") == fake.password:
                    fake.logins += 1
                    fake.token = f"tok{fake.logins}"
                    return self.send(302, headers=[("Location", "/"),
                                                   ("Set-Cookie", f"ESP32SESSION={fake.token}; Path=/")])
                return self.send(302, headers=[("Location", "/?failed")])
            if not self.authed():
                return None
            if path == "/":
                raw = self.body()
                folder_at = raw.find(b'name="folder"')
                file_at = raw.find(b'filename="')
                assert 0 <= folder_at < file_at, "the folder field must precede the file"
                folder = raw[folder_at:].split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0].decode()
                name = raw[file_at + 10:].split(b'"', 1)[0].decode()
                data = raw[file_at:].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
                fake.files.setdefault(folder, {})[name] = len(data)
                fake.uploads.append((folder, name, len(data)))
                return self.send(200, "OK")
            if path == "/bootapp":
                fake.booted = self.form()["label"]
                fake.alive = False
                return self.send(200, "OK")
            if path == "/OTA":
                f = self.form()
                fake.ota = {"size": int(f["size"]), "manifest": json.loads(f["manifest"])}
                return self.send(200, "OK")
            if path == "/OTAFILE":
                n = len(self.body())
                assert n > fake.ota["size"]
                label = fake.ota["manifest"]["sourceName"].rsplit(".", 1)[0][:16]
                fake.entries.append({"type": 0, "subtype": 0x11, "subtypeName": "ota_1",
                                     "label": label, "offset": 0x4E0000, "size": 0x10000,
                                     "running": False})
                return self.send(200, "OK")
            if path == "/partitions":
                f = self.form()
                if f["action"] == "apply":
                    return self.send(200, "OK")
                return self.send(200, json.dumps({**fake.table(), "dirty": True}), "application/json")
            return self.send(302, headers=[("Location", "/")])
    return H


@pytest.fixture
def fake():
    f = Fake()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_for(f))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    f.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield f
    srv.shutdown()


def test_esp_image_size_counts_the_checksum_and_the_appended_hash():
    img = esp_image()
    assert webui.esp_image_size(img) == len(img)
    assert webui.esp_image_size(img + b"\xff" * 4096) == len(img)
    assert webui.esp_image_size(b"\x00" * 64) == 0


def test_the_manifest_of_a_plain_app_and_of_a_merged_image():
    app = esp_image()
    m = webui.ota_manifest(app, "m9mui.bin")
    assert m["parts"] == [{"kind": "app", "label": "app", "subtype": 0, "sourceOffset": 0,
                           "copySize": len(app), "declaredSize": len(app)}]
    merged = merged_image(app)
    m = webui.ota_manifest(merged, "x.factory.bin")
    assert m["parts"][0]["sourceOffset"] == 0x10000 and m["parts"][0]["copySize"] == len(app)
    data = m["parts"][1]
    assert data["label"] == "spiffs" and data["copySize"] == 0 and data["declaredSize"] == 0x1000, \
        "an empty spiffs part is announced with nothing to copy, as the WebUI does"


def test_listing_sizes_parse():
    entries = webui.parse_listing("pa:/:0\nFo:bkp:0\nFi:a:b.bin:4 MB\nFi:s:512 B\nFi:k:3 kB\n")
    assert [e["name"] for e in entries] == ["bkp", "a:b.bin", "s", "k"]
    assert [e["bytes"] for e in entries] == [None, 4 * 1024 ** 2, 512, 3072] and entries[0]["dir"]


def test_sizes_are_written_as_launcher_writes_them():
    assert [webui.human_size(n) for n in (0, 1023, 1024, 1025, 3_941_483, 1024 ** 3 + 1)] == \
        ["0 B", "1023 B", "1 kB", "2 kB", "4 MB", "2 GB"]


def test_facts_without_a_password_read_only_what_needs_no_login(fake):
    f = webui.facts(webui.WebUI(fake.url))
    assert f["launcher"] == {"running": True, "version": "2.10.0", "logged_in": False}
    assert f["apps"] == [{"label": "m9base", "name": "m9base"}] and "slots" not in f
    assert fake.logins == 0


def test_facts_with_a_password(fake):
    f = webui.facts(webui.WebUI(fake.url, password="pw"))
    assert [s["label"] for s in f["slots"]] == ["m9base"], "Launcher's own partition is left out"
    assert f["slots"][0]["data"] == ["spiffs"] and f["flash"]["free"] == 0xB20000
    assert f["nvs"] == {"launcher": 2, "l_apps": 1}


def test_an_old_build_without_the_nvs_page(fake):
    fake.nvs = False
    assert webui.facts(webui.WebUI(fake.url, password="pw"))["nvs"] is None


def test_nothing_answers():
    f = webui.facts(webui.WebUI("http://127.0.0.1:9"))
    assert f == {"launcher": {"running": False, "version": None, "logged_in": False}}


def test_a_stale_session_logs_in_again_once(fake):
    ui = webui.WebUI(fake.url, password="pw", session="stale")
    ui.partitions()
    assert fake.logins == 1 and ui.session == "tok1"
    ui.partitions()
    assert fake.logins == 1, "a good session is reused"


def test_a_wrong_password_is_an_auth_error(fake):
    with pytest.raises(webui.AuthError):
        webui.WebUI(fake.url, password="nope").partitions()


def test_an_unknown_route_is_unsupported(fake):
    ui = webui.WebUI(fake.url, password="pw")
    with pytest.raises(webui.Unsupported):
        ui._call("GET", "/nosuch")


@pytest.fixture
def ctl(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    auth = tmp_path / "config" / "rxrbench" / "launcher.auth"
    auth.parent.mkdir(parents=True)
    auth.write_text("admin:pw")
    auth.chmod(0o600)
    monkeypatch.setattr(launcherctl.time, "sleep", lambda s: None)
    # a registry that knows one board, `cyd`, by the fake's address: resolve prints it, guard agrees
    host = fake.url[len("http://"):]
    devreg = tmp_path / "devreg.py"
    devreg.write_text("import sys\n"
                      f"open({str(tmp_path / 'devreg.calls')!r}, 'a').write(' '.join(sys.argv[1:]) + chr(10))\n"
                      "if sys.argv[-1] != 'cyd' and sys.argv[-2] != 'cyd': sys.exit(1)\n"
                      f"print({host!r}) if sys.argv[1] == 'resolve' else None\n")
    return lambda *a: launcherctl.main(["--board", "cyd", "--devreg", str(devreg), *a])


def test_a_write_by_address_is_refused_and_a_board_is_resolved_and_guarded(ctl, fake, tmp_path, capsys):
    with pytest.raises(SystemExit, match="a write names a registered board"):
        launcherctl.main(["--url", fake.url, "boot", "camstr"])
    assert launcherctl.main(["--url", fake.url, "status"]) == 0, "reading by address is fine"
    assert ctl("status") == 0
    calls = (tmp_path / "devreg.calls").read_text().splitlines()
    assert calls[0] == "resolve --route wifi cyd" and calls[1].startswith("guard --route wifi cyd ")
    with pytest.raises(SystemExit, match="no wifi route"):
        launcherctl.main(["--board", "nosuch", "--devreg", str(tmp_path / "devreg.py"), "status"])


def test_an_old_build_without_ping_is_still_read_as_launcher(fake):
    fake.alive = False
    f = webui.facts(webui.WebUI(fake.url))
    assert f["launcher"]["running"] and f["launcher"]["old_build"] and f["launcher"]["version"] == "2.10.0"
    assert f["apps"] == [] and "slots" not in f


def test_the_session_is_kept_mode_600_and_reused(ctl, fake, tmp_path, capsys):
    assert ctl("status") == 0
    assert ctl("status") == 0
    assert fake.logins == 1
    path = next((tmp_path / "cache" / "rxrbench" / "launcher").iterdir())
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    out = capsys.readouterr().out
    assert "m9base" in out and "tok" not in out and "pw" not in out


def test_put_uploads_once_and_reads_it_back(ctl, fake, tmp_path, capsys):
    img = tmp_path / "m9mui.bin"
    img.write_bytes(b"\x01" * 300000)
    assert ctl("put", str(img), "/") == 0
    assert ctl("put", str(img), "/") == 0
    assert len(fake.uploads) == 1, "the same name at the same size is left alone"
    assert "unchanged" in capsys.readouterr().out
    assert ctl("put", str(img), "/", "--force") == 0 and len(fake.uploads) == 2


def test_rm_reads_back(ctl, fake):
    assert ctl("rm", "/old.bin") == 0
    assert "old.bin" not in fake.files["/"]


def test_boot_refuses_an_unknown_slot_and_boots_a_known_one(ctl, fake):
    with pytest.raises(SystemExit, match="no slot"):
        ctl("boot", "nosuch")
    assert fake.booted is None
    assert ctl("boot", "m9base") == 0 and fake.booted == "m9base"


def test_install_by_upload_finds_the_new_slot(ctl, fake, tmp_path, capsys):
    img = tmp_path / "m9muip.bin"
    img.write_bytes(esp_image())
    assert ctl("install", str(img)) == 0
    assert fake.ota["manifest"]["sourceName"] == "m9muip.bin"
    assert "installed into slot m9muip" in capsys.readouterr().out


def test_install_refuses_when_the_slot_does_not_fit(ctl, fake, tmp_path):
    fake.free = [{"offset": 0x4E0000, "size": 0x10000}]
    img = tmp_path / "big.bin"
    img.write_bytes(esp_image(b"\x22" * 0x20000))
    with pytest.raises(SystemExit, match="unallocated"):
        ctl("install", str(img))
    assert fake.ota is None, "nothing was announced to the device"


def test_partition_edits_need_confirm(ctl, fake):
    with pytest.raises(SystemExit, match="--confirm"):
        ctl("part", "delete", "label=m9base")
    assert ctl("part", "discard") == 0
    assert ctl("part", "delete", "label=m9base", "--confirm") == 0


def test_an_auth_file_readable_by_others_is_refused(tmp_path):
    p = tmp_path / "auth"
    p.write_text("admin:pw")
    os.chmod(p, 0o644)
    with pytest.raises(webui.AuthError, match="chmod 600"):
        webui.read_secret_file(p)
    os.chmod(p, 0o600)
    assert webui.read_secret_file(p) == ("admin", "pw")
