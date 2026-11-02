"""A client for the WebUI of bmorcelli's Launcher, the ESP32 app manager.

Launcher is a firmware that lives in its own partition and installs other firmware images
beside it: each image gets its own app partition (an OTA slot, labelled from the file name)
and, if it brings one, its own data partition. A power-on reset always boots Launcher; any
other reset follows the otadata like a stock ESP-IDF bootloader. While Launcher runs, it can
serve a small web interface on WiFi, and that interface is all this module speaks:

    GET  /ping                         "launcher-pong", no login needed
    GET  /systeminfo                   version, SD usage, installed apps; no login needed
    POST /login                        username + password; answers 302 with a session cookie
    GET  /partitions                   the partition table as JSON (running slot, free ranges)
    POST /partitions action=...        resize / create / delete / format / apply / discard /
                                       backup / restore: edits a draft; "apply" writes the
                                       table and reboots (moving partitions takes minutes)
    GET  /nvs                          every NVS key (blob sizes only, never their bytes);
                                       newer builds only
    GET  /listfiles?folder=            one line per entry: "Fo:name:0" / "Fi:name:<size text>"
    GET  /file?name=&action=           download / delete / create (a folder)
    POST /  (multipart)                upload into the SD card; a "folder" field first
    POST /UPDATE fileName=             install an image from the SD card into a new slot
    POST /OTA command=0 size= manifest=  then  POST /OTAFILE (multipart): install an uploaded
                                       image into a new slot, without the SD card
    POST /bootapp label=               set the otadata to that slot and reboot into it
    GET  /reboot

Three things about it shape this client:

- **One session at a time.** A login clears every other session (a browser open on the
  device is logged out) and stores the new token in NVS. A caller that keeps the cookie
  (`WebUI.session`) and offers it again saves both.
- **An unknown route answers 302 to "/"**, not 404, so an endpoint an older build lacks shows
  up as a redirect; `Unsupported` says so.
- **Only while Launcher runs.** Once an installed image is booted, the device is that image;
  nothing here reaches it.

Standard library only: this runs on a bench host as part of a module.
"""
import http.client
import json
import os
import re
import secrets
import urllib.parse

from ansible_collections.rxrbench.core.plugins.module_utils import bootid, esppart

PONG = "launcher-pong"
SIZE_UNITS = {"B": 1, "kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}
ESP_IMAGE_MAGIC = 0xE9


class LauncherError(Exception):
    """The device answered something other than what the request needs."""


class Unsupported(LauncherError):
    """This Launcher build has no such endpoint (it redirected to "/")."""


class AuthError(LauncherError):
    """The login was refused, or the session is no longer valid."""


class WebUI:
    """One Launcher WebUI. `base` is `http://host[:port]`. A `session` token from an earlier
    login is tried before logging in again."""

    def __init__(self, base, username="admin", password=None, session=None, timeout=15):
        url = urllib.parse.urlsplit(base if "://" in base else f"http://{base}")
        if url.scheme != "http":
            raise LauncherError(f"Launcher serves plain http, not {url.scheme}")
        self.host = url.hostname
        self.port = url.port or 80
        self.username = username
        self.password = password
        self.session = session
        self.timeout = timeout

    # -- transport --------------------------------------------------------------------

    def _request(self, method, path, body=None, headers=None, timeout=None, auth=True):
        hdrs = dict(headers or {})
        if auth and self.session:
            hdrs["Cookie"] = f"ESP32SESSION={self.session}"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        except OSError as e:
            raise LauncherError(f"{self.host}:{self.port}: {e}") from e
        finally:
            conn.close()

    def _call(self, method, path, body=None, headers=None, timeout=None):
        """An authenticated request: logs in when there is no session or it was refused."""
        if not self.session:
            self.login()
        status, hdrs, data = self._request(method, path, body, headers, timeout)
        if status == 401:
            self.login()
            status, hdrs, data = self._request(method, path, body, headers, timeout)
        if status == 401:
            raise AuthError("the session was refused right after a login")
        if status == 302 and hdrs.get("location", "").startswith("/"):
            raise Unsupported(f"{method} {path.split('?')[0]}: not served by this Launcher build")
        if status != 200:
            raise LauncherError(f"{method} {path.split('?')[0]}: HTTP {status}: "
                                f"{data[:200].decode(errors='replace').strip()}")
        return data

    @staticmethod
    def _form(fields):
        return (urllib.parse.urlencode(fields).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"})

    # -- no login ---------------------------------------------------------------------

    def ping(self):
        """True when a Launcher WebUI answers here."""
        try:
            status, _h, data = self._request("GET", "/ping", auth=False, timeout=5)
        except LauncherError:
            return False
        return status == 200 and data.decode(errors="replace").strip() == PONG

    def systeminfo(self):
        """{"VERSION", "SD": {free, used, total, *Bytes}, "APPS": [{label, name}]}."""
        status, _h, data = self._request("GET", "/systeminfo", auth=False)
        if status != 200:
            raise LauncherError(f"GET /systeminfo: HTTP {status}")
        return _json(data, "/systeminfo")

    # -- session ----------------------------------------------------------------------

    def login(self):
        if self.password is None:
            raise AuthError("no WebUI password given")
        body, hdrs = self._form({"username": self.username, "password": self.password})
        status, rh, _d = self._request("POST", "/login", body, hdrs, auth=False)
        m = re.search(r"ESP32SESSION=([^;]+)", rh.get("set-cookie", ""))
        if status != 302 or not m:
            raise AuthError("the WebUI refused the login")
        self.session = m.group(1)
        return self.session

    # -- reads ------------------------------------------------------------------------

    def partitions(self):
        return _json(self._call("GET", "/partitions"), "/partitions")

    def nvs(self):
        """Every NVS key by namespace, or None on a build without /nvs."""
        try:
            return _json(self._call("GET", "/nvs"), "/nvs")
        except Unsupported:
            return None

    def list_files(self, folder="/"):
        """[{name, dir, size, bytes}] in `folder`; `size` is the WebUI's own text (rounded up to a
    whole unit), `bytes` the upper bound it means."""
        text = self._call("GET", "/listfiles?" + urllib.parse.urlencode({"folder": folder}))
        return parse_listing(text.decode(errors="replace"))

    def download(self, path):
        return self._call("GET", "/file?" + urllib.parse.urlencode({"name": path,
                                                                     "action": "download"}),
                          timeout=max(self.timeout, 120))

    # -- writes -----------------------------------------------------------------------

    def delete_file(self, path):
        reply = self._call("GET", "/file?" + urllib.parse.urlencode({"name": path,
                                                                      "action": "delete"}))
        text = reply.decode(errors="replace")
        if not text.startswith("Deleted"):
            raise LauncherError(f"delete {path}: {text.strip()}")

    def mkdir(self, path):
        self._call("GET", "/file?" + urllib.parse.urlencode({"name": path, "action": "create"}))

    def upload(self, data, name, folder="/"):
        """Put `data` on the SD card as `folder`/`name`."""
        body, ctype = multipart([("folder", None, folder.encode())], ("file", name, data))
        reply = self._call("POST", "/", body, {"Content-Type": ctype},
                           timeout=max(self.timeout, 60 + len(data) // 20000))
        if reply.decode(errors="replace").strip() != "OK":
            raise LauncherError(f"upload {name}: {reply[:200]!r}")

    def boot_app(self, label):
        """Make the slot `label` the one the otadata names, and reboot into it. Launcher writes
        the otadata entry without a rollback state, so the image is not on probation."""
        body, hdrs = self._form({"label": label})
        reply = self._call("POST", "/bootapp", body, hdrs).decode(errors="replace").strip()
        if reply != "OK":
            raise LauncherError(f"boot {label}: {reply}")

    def reboot(self):
        self._call("GET", "/reboot")

    def install_from_sd(self, path):
        """Install the image at `path` on the SD card into a new slot. Launcher answers at once
        and copies in its main loop; the slot appears in /partitions when it is done."""
        body, hdrs = self._form({"fileName": path})
        reply = self._call("POST", "/UPDATE", body, hdrs).decode(errors="replace")
        if "Starting" not in reply:
            raise LauncherError(f"install {path}: {reply.strip()}")

    def install_upload(self, data, source_name):
        """Install an image straight from this host into a new slot, as the WebUI's OTA page
        does: announce it with a manifest of its parts, then stream it."""
        manifest = ota_manifest(data, source_name)
        body, hdrs = self._form({"command": 0, "size": len(data), "manifest": json.dumps(manifest)})
        reply = self._call("POST", "/OTA", body, hdrs).decode(errors="replace").strip()
        if reply != "OK":
            raise LauncherError(f"install {source_name}: {reply}")
        body, ctype = multipart([], ("file1", source_name, data))
        self._call("POST", "/OTAFILE", body, {"Content-Type": ctype},
                   timeout=max(self.timeout, 120 + len(data) // 20000))
        return manifest

    def partition_action(self, action, **params):
        """One partition-manager action. Every action but apply only edits Launcher's draft
        (the reply is the draft table); `apply` writes it and reboots."""
        body, hdrs = self._form({"action": action, **params})
        data = self._call("POST", "/partitions", body, hdrs, timeout=max(self.timeout, 60))
        return None if action == "apply" else _json(data, f"/partitions {action}")


# -- pure helpers (no device) -----------------------------------------------------------

def _json(data, what):
    try:
        return json.loads(data)
    except ValueError as e:
        raise LauncherError(f"{what}: not JSON: {data[:120]!r}") from e


def human_size(n):
    """A size as the WebUI's listing writes it: a whole number of the largest unit below it,
    rounded UP ("512 B", "3 kB", "4 MB"). Equal text is the best equality a listing allows."""
    for unit, scale in (("B", 1), ("kB", 1024), ("MB", 1024 ** 2)):
        if n < scale * 1024:
            return f"{n} B" if unit == "B" else f"{-(-n // scale)} {unit}"
    return f"{-(-n // 1024 ** 3)} GB"


def parse_size(text):
    """The WebUI's size text as bytes: an upper bound, since the device rounds up. None when
    it is not one."""
    m = re.fullmatch(r"\s*(\d+)\s*(B|kB|MB|GB)\s*", text or "")
    return int(m.group(1)) * SIZE_UNITS[m.group(2)] if m else None


def parse_listing(text):
    out = []
    for line in text.splitlines():
        kind, sep, rest = line.partition(":")
        if not sep or kind not in ("Fo", "Fi"):
            continue
        name, _sep, size = rest.rpartition(":")
        out.append({"name": name, "dir": kind == "Fo",
                    "size": None if kind == "Fo" else size, "bytes": parse_size(size)})
    return out


def multipart(fields, filepart):
    """A multipart/form-data body: plain `fields` first (Launcher reads a "folder" field only
    if it precedes the file), then one file part."""
    boundary = "rxrbench" + secrets.token_hex(12)
    chunks = []
    for name, _fn, value in fields:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                      .encode() + value + b"\r\n")
    name, filename, data = filepart
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                  f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                  .encode() + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def esp_image_size(data, offset=0):
    """The length of the ESP-IDF app image at `offset`: header, segments, the checksum byte
    ending a 16-byte block, and the appended SHA-256 when the header says so (the core's
    reading, `bootid.esp_image_length`). 0 when it is not one."""
    return bootid.esp_image_length(data[offset:]) or 0


def ota_manifest(data, source_name):
    """The manifest the WebUI's OTA page sends for a file: one app part, plus the data
    partitions a merged (factory) image carries. Mirrors the page's own analysis, so a
    device sees the same request a browser would send."""
    parts = []
    app_offset, app_size = 0, 0
    rows = esppart.parse(data, esppart.OFFSET)
    if rows and rows[0]["type"] == esppart.DATA:     # the page takes a merged image by this
        for r in rows:
            ptype, sub, off, size, label = r["type"], r["subtype"], r["offset"], r["size"], r["label"]
            if ptype == 0x00 and sub in (0x00, 0x10, 0x20):
                app_offset = off or 0x10000
                app_size = min(size, len(data) - app_offset)
                measured = esp_image_size(data, app_offset)
                if measured and measured < app_size:
                    app_size = measured
            elif ptype == 0x01 and sub in (0x81, 0x82, 0x83) and off < len(data):
                copy = min(size, len(data) - off)
                spiffs_like = sub in (0x82, 0x83)
                if spiffs_like and data[off:off + 16] in (b"\xff" * 16, b"\0" * 16):
                    copy = 0
                if copy > 0 or (spiffs_like and size > 0):
                    parts.append({"kind": "data", "label": label, "subtype": sub,
                                  "sourceOffset": off, "copySize": copy, "declaredSize": size})
    else:
        app_size = esp_image_size(data, 0) or len(data)
    if app_size <= 0:
        raise LauncherError(f"{source_name}: no ESP32 app image in it")
    parts.insert(0, {"kind": "app", "label": "app", "subtype": 0, "sourceOffset": app_offset,
                     "copySize": app_size, "declaredSize": app_size})
    return {"sourceName": source_name, "parts": parts}


def slots(partitions):
    """The app slots of a /partitions reply: [{label, app, offset, size, running, data}]."""
    out = []
    for e in partitions.get("entries", []):
        if e.get("type") != 0 or e.get("subtype") == 0x20:     # 0x20 = test: Launcher itself
            continue
        out.append({"label": e.get("label"), "app": e.get("appName") or e.get("label"),
                    "subtype": e.get("subtypeName"), "offset": e.get("offset"),
                    "size": e.get("size"), "running": bool(e.get("running")),
                    "data": e.get("dataLabels") or []})
    return out


def free_bytes(partitions):
    return sum(r.get("size", 0) for r in partitions.get("freeRanges", []))


def facts(ui):
    """What launcher_facts reports: the parts that need no login, then (with a password or a
    session) the slots, the free flash and the NVS key counts."""
    out = {"launcher": {"running": False, "version": None, "logged_in": False}}
    if not ui.ping():
        # A build from before /ping (2.6.x) answers 302 there and has /systeminfo all the same.
        try:
            info = ui.systeminfo()
        except LauncherError:
            return out
        out["launcher"].update(running=True, version=info.get("VERSION"), old_build=True)
        sd = info.get("SD") or {}
        out["sd"] = {k: sd.get(k) for k in ("totalBytes", "usedBytes", "freeBytes")}
        out["apps"] = []
        return out
    info = ui.systeminfo()
    sd = info.get("SD") or {}
    out["launcher"].update(running=True, version=info.get("VERSION"))
    out["sd"] = {k: sd.get(k) for k in ("totalBytes", "usedBytes", "freeBytes")}
    out["apps"] = [{"label": a.get("label"), "name": a.get("name")} for a in info.get("APPS", [])]
    if ui.password is None and not ui.session:
        return out
    parts = ui.partitions()
    out["launcher"]["logged_in"] = True
    out["slots"] = slots(parts)
    out["flash"] = {"size": parts.get("flashSize"), "free": free_bytes(parts),
                    "dirty": bool(parts.get("dirty"))}
    nvs = ui.nvs()
    out["nvs"] = None if nvs is None else {ns: len(keys) for ns, keys in nvs.items()}
    return out


def read_secret_file(path):
    """`user:password` (or a bare password) from a file only its owner may read."""
    st = os.stat(path)
    if st.st_mode & 0o077:
        raise AuthError(f"{path} is readable by others (chmod 600 it)")
    with open(path, encoding="utf-8") as fh:
        text = fh.read().strip()
    user, sep, pw = text.partition(":")
    return (user, pw) if sep else ("admin", text)
