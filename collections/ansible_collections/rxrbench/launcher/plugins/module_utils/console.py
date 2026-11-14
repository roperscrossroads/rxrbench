"""Launcher's serial console, through a board's broker in the `launcher` dialect.

Launcher (bmorcelli's app manager) serves a line console at 115200 while it runs: nothing
typed is echoed, a line ends at a newline, and an action answers `OK ...` or `ERR ...`.
`version` answers `Launcher <ver>` on builds after 2.9.1 (a 2.9.1 console answers `ERR unknown
command`, which still says Launcher has the port; its version is then NO_VERSION); `partitions` prints the table (a subtype as a name only for `ota_N`, `fat`, `spiffs` and
`littlefs`, else as hex: Launcher's own partition is `0x20`, test, or `0x00`, factory; a size as
`20KB`, `3MB`), the otadata slot marked `<-- BOOT`, ending in a `Flash size:` line; `settings get` prints the settings as one JSON
line (the WebUI password among them: the broker's log rule hides it, this module never
returns it); `settings set <json>` replaces them; `flash firmware <name> <size>` installs an
app from raw bytes streamed after `READY` (`serialflash.py`, on a released port).

The console exists only while Launcher runs. A power-on boots Launcher, which shows a boot
window (`Press the button to enter the Launcher!`, 1 s plus `bootTimer` seconds with
`bootToApp` on) and then boots the selected app, after which the port is the app's. `enter()`
gets to Launcher from either state: it asks `version`; when nothing answers it has the broker
attend the banner (answering it once: `ENTRIES`), pulses the modem lines (a power-on to
Launcher's bootloader) and waits for `version`. `leave()` reboots, which boots the selected
app directly (a software reset follows the otadata).

The console is live during the window too (the console task runs from the start), so the
banner may be answered with a key press, `nav SelPress`, or with `settings set` turning
`bootToApp` off: Launcher merges the keys sent, applies them at once, and the boot loop ends in
the menu instead of the app. A build whose input task clears the key before the boot loop reads
it (the ThinkNode M9's) keeps the press only by luck; the setting always holds. `enter()` puts
`bootToApp` back on once the menu is there (a board that boots its app had it on).
"""
import json
import re
import time

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client

DIALECT = "launcher"
#: What the boot banner is answered with, to stay in Launcher: a key press (the default) or
#: the setting (`launcher_enter: settings` on the board's type).
#: The flag is JSON `false`: Launcher reads it with ArduinoJson's `is<bool>()`, which skips a 0
#: or 1 (every release, 2.9.1 to 3.0.0) and still answers `OK settings loaded`.
ENTRIES = {"selpress": "nav SelPress", "settings": 'settings set {"bootToApp":false}'}
#: Seconds after the power-on by which a window ended by `bootToApp` off has closed (a 2.9.1
#: window is 2 s + 3 s with bootToApp on; off, it ends at 2 s); the setting is put back then.
SETTLE = 3.0
#: Seconds after the power-on by which a boot window with bootToApp on has closed (2.9.1: 2 s +
#: 3 s; a CYD with bootTimer 4: 5 s). Launcher's console answers inside the window too, so a
#: `version` reply is not the menu: the banner is attended until then.
WINDOW = 6.0
#: Commands that carry a secret after these words: a WiFi password, the settings JSON (the
#: WebUI password is in it). The broker keeps everything after the words out of its log.
SECRET_PREFIXES = ("wifi connect ", "wifi add ", "settings set ")
broker_client.secret_shapes(prefixes=SECRET_PREFIXES)

VERSION_RE = re.compile(r"^Launcher (\S+)")
PART_RE = re.compile(r"^(?P<label>\S+)\s+type=(?P<type>\S+)\s+subtype=(?P<sub>\S+)\s+"
                     r"offset=0x(?P<off>[0-9A-Fa-f]+)\s+size=0x(?P<size>[0-9A-Fa-f]+)"
                     r"(?P<boot>.*<-- BOOT)?", re.M)
FREE_RE = re.compile(r"^<free>\s+offset=0x(?P<off>[0-9A-Fa-f]+)\s+size=0x(?P<size>[0-9A-Fa-f]+)", re.M)
FLASH_RE = re.compile(r"^Flash size: (?P<flash>\S+), free total: (?P<free>\S+)", re.M)
#: The settings this tool reads and may write; the rest (colours, brightness, paths, a
#: per-board entry keyed by the MAC) are the device's own. `dev` is Launcher's developer-mode
#: flag, a bool. `bootTimer` and `DDLB` exist from the betas after 2.9.1 on. The WebUI password
#: is compared, never returned.
SETTINGS_KEYS = {"bootToApp": "boot_to_app", "bootTimer": "boot_timer", "DDLB": "deep_sleep_to_launcher",
                 "wui_usr": "wui_user", "dev": "dev_mode", "autoConnect": "auto_connect",
                 "onlyBins": "only_bins"}
#: What Launcher prints for a subtype it has no name for: the ESP-IDF numbers.
APP_SUBTYPES = {0x00: "factory", 0x20: "test"}
DATA_SUBTYPES = {0x00: "ota", 0x01: "phy", 0x02: "nvs", 0x03: "coredump", 0x04: "nvs_keys",
                 0x05: "efuse", 0x81: "fat", 0x82: "spiffs", 0x83: "littlefs"}


class ConsoleError(Exception):
    """Launcher's console did not answer as expected."""


# -- parsers (no device) ------------------------------------------------------------------

def parse_version(text):
    m = VERSION_RE.search(text or "")
    return m.group(1) if m else None


def human_bytes(text):
    """Launcher's size text as bytes: `16MB`, `20KB` (a release), `4Mb`, `1344Kb` (the betas'
    WebUI), `640 bytes`, `640B`."""
    m = re.fullmatch(r"\s*(\d+)\s*(MB|KB|GB|bytes|B)\s*", text or "", re.I)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"gb": 1 << 30, "mb": 1 << 20, "kb": 1 << 10, "bytes": 1, "b": 1}[unit]


def subtype_name(ptype, text):
    """The subtype as this tool names it, from what Launcher printed: a name as it is
    (`ota_0`, `spiffs`, `test`), a hex number by the ESP-IDF table (`0x20` -> `test`)."""
    if not text.lower().startswith("0x"):
        return text
    try:
        n = int(text, 16)
    except ValueError:
        return text
    if ptype == "app":
        return APP_SUBTYPES.get(n, f"ota_{n - 0x10}" if 0x10 <= n <= 0x1F else text)
    return DATA_SUBTYPES.get(n, text)


def parse_partitions(text):
    """The `partitions` printout: {manager, apps, data, free, flash_size, boot}. `apps` are the
    installed images in flash order ({label, subtype, offset, size, boot}); `boot` is the
    label the otadata selects, or None when no app is selected."""
    manager, apps, data, boot = None, [], [], None
    for m in PART_RE.finditer(text or ""):
        sub = subtype_name(m["type"], m["sub"])
        row = {"label": m["label"], "subtype": sub, "offset": int(m["off"], 16),
               "size": int(m["size"], 16)}
        if m["type"] == "app":
            if sub in ("test", "factory") and manager is None:
                manager = row
                continue
            row["boot"] = bool(m["boot"])
            if row["boot"]:
                boot = row["label"]
            apps.append(row)
        elif m["type"] == "data" and row["offset"] >= 0x10000:
            data.append(row)
    free = [{"offset": int(m["off"], 16), "size": int(m["size"], 16)} for m in FREE_RE.finditer(text or "")]
    m = FLASH_RE.search(text or "")
    return {"manager": manager, "apps": apps, "data": data, "free": free,
            "flash_size": human_bytes(m["flash"]) if m else None,
            "free_total": human_bytes(m["free"]) if m else sum(f["size"] for f in free),
            "boot": boot}


#: Launcher keeps one settings entry keyed by the eFuse MAC and the board name of the board that
#: SAVED the settings file (`02:00:00:00:00:01-Lilygo T-Embed CC1101`: its screen rotation). A
#: `config.conf` carried from one board to another (an SD card moved, a file copied) carries that
#: board's key, so this names the board that last saved the settings, not the chip on the port:
#: the chip's own identity is its eFuse MAC, read by esptool with a reset (the deep survey).
BOARD_KEY_RE = re.compile(r"^((?:[0-9a-f]{2}:){5}[0-9a-f]{2})-(.+)$", re.I)


def settings_owner(settings_json):
    """(mac, board name) of the board that saved these settings, from the per-board entry of a
    `settings get` JSON, or (None, None) when the file has never been saved on any board."""
    for key in settings_json:
        m = BOARD_KEY_RE.match(str(key))
        if m:
            return m.group(1).lower(), m.group(2)
    return None, None


def parse_settings(text):
    """The `settings get` JSON as this tool's words, the passwords left out: {boot_to_app,
    boot_timer, ..., wifi: [ssid, ...]}, which board saved the settings (`_settings_mac`,
    `_settings_board`, from Launcher's per-board entry) and, apart, what it needs to compare a declared password
    (`_wui_password`, `_wifi_passwords` {ssid: pwd}): the caller lifts or drops the `_` keys."""
    try:
        j = json.loads(text)
    except ValueError as e:
        raise ConsoleError(f"the settings are not JSON: {text[:80]!r}") from e
    out = {ours: j.get(theirs) for theirs, ours in SETTINGS_KEYS.items() if theirs in j}
    for key in ("boot_to_app", "deep_sleep_to_launcher", "auto_connect", "only_bins", "dev_mode"):
        if key in out and out[key] is not None:
            out[key] = bool(out[key])
    nets = [w for w in (j.get("wifi") or []) if isinstance(w, dict) and w.get("ssid")]
    out["wifi"] = [w["ssid"] for w in nets]
    out["_wui_password"] = j.get("wui_pwd")
    out["_wifi_passwords"] = {w["ssid"]: w.get("pwd") for w in nets}
    out["_settings_mac"], out["_settings_board"] = settings_owner(j)
    return out


def public(settings):
    return {k: v for k, v in settings.items() if not k.startswith("_")}


# -- the console --------------------------------------------------------------------------

def exec_line(board, cmd, deadline=8.0, expect_reply=True):
    return board.exec(cmd, deadline=deadline, dialect=DIALECT, expect_reply=expect_reply)


#: What a Launcher console answers `version` with when the build has no such command (releases
#: up to 2.9.1): proof enough that Launcher has the port, with its version unreadable here.
UNKNOWN_COMMAND = "ERR unknown command"
NO_VERSION = "unknown"


def probe(board, deadline=3.0):
    """Is Launcher's console there: its version (`Launcher <ver>`), NO_VERSION when a build
    without the `version` command answered (`ERR unknown command`), None when nothing of
    Launcher's answers (the app has the port)."""
    try:
        out = exec_line(board, "version", deadline)
    except broker_client.BrokerError as e:
        if "timeout" in str(e) or "no echo" in str(e):
            return None
        raise
    v = parse_version(out)
    if v:
        return v
    return NO_VERSION if out.startswith(UNKNOWN_COMMAND) else None


def version(board, deadline=3.0):
    """`probe()`: Launcher's version, NO_VERSION on a build without the command, or None."""
    return probe(board, deadline)


def enter(board, wait=25.0, poll=1.0, entry="selpress"):
    """Launcher's version, with the board in Launcher: as it is when Launcher answers, else
    after a power-on the broker attends (answering the boot banner once with `ENTRIES[entry]`).
    Returns (version, how): `running` (it already was), `entered` (reset into it: `leave()`
    puts the app back). The `settings` entry turns bootToApp off for the boot and back on
    once the window has closed (`SETTLE`), so the board reads as it was."""
    if entry not in ENTRIES:
        raise ConsoleError(f"{board.board}: unknown entry {entry!r}: one of {', '.join(ENTRIES)}")
    v = version(board)
    if v:
        return v, "running"
    r = board.request(op="attend", dialect=DIALECT, ttl=wait + 5, answer=ENTRIES[entry])
    if not r.get("ok"):
        raise ConsoleError(f"{board.board}: the broker cannot attend Launcher's banner: {r.get('error')}")
    r = board.request(op="reset")
    if not r.get("ok"):
        board.request(op="attend", dialect=DIALECT, ttl=0)
        raise ConsoleError(f"{board.board}: the port cannot reset the board ({r.get('error')}): "
                           "enter Launcher by hand (power-on, then its button) and run again")
    t0 = time.monotonic()
    end = t0 + wait
    while time.monotonic() < end:
        time.sleep(poll)
        v = version(board, deadline=2.0)
        if v:
            # the console answers inside the boot window as well: keep the banner attended until
            # the window has closed, then ask again (an unanswered window boots the app)
            time.sleep(max(0.0, t0 + WINDOW - time.monotonic()))
            board.request(op="attend", dialect=DIALECT, ttl=0)
            if not version(board, deadline=2.0):
                raise ConsoleError(f"{board.board}: Launcher answered inside its boot window, then the "
                                   f"app booted: the banner went unanswered within {WINDOW:.0f} s")
            if entry == "settings":
                time.sleep(max(0.0, t0 + SETTLE - time.monotonic()))
                set_settings(board, {"bootToApp": True})
                if not version(board, deadline=2.0):
                    raise ConsoleError(f"{board.board}: the app booted after the settings entry: the boot "
                                       f"window outlasted {SETTLE:.0f} s and bootToApp was put back too soon")
            return v, "entered"
    board.request(op="attend", dialect=DIALECT, ttl=0)
    raise ConsoleError(f"{board.board}: no Launcher console answered within {wait:.0f} s of a reset: "
                       "the board boots its app with no window (a key-boot build), or Launcher is "
                       "not on it")


def leave(board):
    """Boot the selected app now (a software reset follows the otadata)."""
    out = exec_line(board, "reboot", deadline=5.0)
    if not out.startswith("OK"):
        raise ConsoleError(f"{board.board}: reboot: {out}")


def read(board, deadline=8.0):
    """What Launcher holds, read through its console: version (NO_VERSION on a build without
    the command), the table, the settings (the passwords apart, under keys the caller drops),
    and which board saved the settings, as Launcher's per-board entry says (`settings_mac`,
    `settings_board`: the board that wrote the file, not necessarily the chip on the port)."""
    v = probe(board, deadline)
    if not v:
        raise ConsoleError(f"{board.board}: `version` did not answer as Launcher")
    table = parse_partitions(exec_line(board, "partitions", deadline))
    if table["manager"] is None:
        raise ConsoleError(f"{board.board}: the partition table shows no Launcher partition")
    settings = parse_settings(exec_line(board, "settings get", deadline))
    return {"version": v, **table, "settings": settings,
            "settings_mac": settings.get("_settings_mac"), "settings_board": settings.get("_settings_board")}


def set_settings(board, changes, deadline=8.0):
    """`settings set` with only `changes` (Launcher's own keys): the device merges them
    (`loadSettingsJson` copies each key sent over the saved root, applies and persists)."""
    out = exec_line(board, "settings set " + json.dumps(changes, separators=(",", ":")), deadline)
    if not out.startswith("OK"):
        raise ConsoleError(f"{board.board}: settings set: {out}")


def facts(board, wui_password=None, deadline=8.0, wifi_psks=None):
    """`read()` as a module returns it: the settings without their passwords, whether the WebUI
    password is the declared one (None when none was declared) and, per declared network,
    whether its password is."""
    got = read(board, deadline)
    settings = got["settings"]
    out = {**got, "settings": public(settings)}
    out["wui_password_matches"] = (None if wui_password is None
                                   else settings.get("_wui_password") == wui_password)
    have = settings.get("_wifi_passwords") or {}
    out["wifi_psk_matches"] = {ssid: have.get(ssid) == psk for ssid, psk in (wifi_psks or {}).items()}
    return out
