"""The Launcher plugin's console module: what it makes of Launcher's printouts, and how it gets
to Launcher from a board that is running one of its apps (a fake console behind a real broker)."""
import importlib.util
import os
import threading
import time

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.launcher.plugins.module_utils import console
from tests.support.fake_launcher import FakeLauncher

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("broker_for_launcher", os.path.join(
    REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "broker.py"))
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)
mb.load_dialects(os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "launcher",
                              "host", "dialects.d"))

TABLE = """== Partition table ==
nvs              type=data subtype=nvs       offset=0x009000 size=0x005000 (20Kb)
otadata          type=data subtype=ota       offset=0x00E000 size=0x002000 (8Kb)
test             type=app  subtype=test      offset=0x010000 size=0x150000 (1344Kb)
camstr           type=app  subtype=ota_0     offset=0x160000 size=0x100000 (1Mb)  <-- BOOT
wifite           type=app  subtype=ota_1     offset=0x260000 size=0x0E0000 (896Kb)
spiffs           type=data subtype=spiffs    offset=0x340000 size=0x080000 (512Kb)
<free>           offset=0x3C0000 size=0x040000 (256Kb)
Flash size: 4Mb, free total: 256Kb
"""


def test_the_partition_printout_gives_the_manager_the_apps_and_the_boot_slot():
    t = console.parse_partitions(TABLE)
    assert t["manager"] == {"label": "test", "subtype": "test", "offset": 0x10000, "size": 0x150000}
    assert [a["label"] for a in t["apps"]] == ["camstr", "wifite"]
    assert t["apps"][0]["boot"] is True and t["apps"][1]["boot"] is False and t["boot"] == "camstr"
    assert t["data"] == [{"label": "spiffs", "subtype": "spiffs", "offset": 0x340000, "size": 0x80000}]
    assert t["free"] == [{"offset": 0x3C0000, "size": 0x40000}]
    assert t["flash_size"] == 4 << 20 and t["free_total"] == 256 << 10


def test_a_fresh_launcher_has_no_apps_and_selects_none():
    t = console.parse_partitions("\n".join(TABLE.splitlines()[:4]) + "\nFlash size: 4Mb, free total: 2752Kb\n")
    assert t["apps"] == [] and t["boot"] is None and t["manager"]["label"] == "test"


def test_sizes_as_launcher_prints_them():
    assert console.human_bytes("4Mb") == 4 << 20
    assert console.human_bytes("1344Kb") == 1344 << 10
    assert console.human_bytes("640 bytes") == 640
    assert console.human_bytes("lots") is None


def test_settings_are_read_in_this_tools_words_with_the_passwords_apart():
    s = console.parse_settings('{"onlyBins":1,"bootToApp":1,"bootTimer":4,"DDLB":1,"wui_usr":"admin",'
                               '"wui_pwd":"launcher","dev":false,"autoConnect":0,'
                               '"wifi":[{"ssid":"lab-iot","pwd":"hunter22"},{"ssid":"open"}],"bright":100}')
    assert s["boot_to_app"] is True and s["boot_timer"] == 4 and s["auto_connect"] is False
    assert s["wui_user"] == "admin" and s["dev_mode"] is False and s["wifi"] == ["lab-iot", "open"]
    assert s["_wui_password"] == "launcher" and s["_wifi_passwords"] == {"lab-iot": "hunter22", "open": None}
    pub = console.public(s)
    assert not any(k.startswith("_") for k in pub) and "bright" not in pub
    with pytest.raises(console.ConsoleError, match="not JSON"):
        console.parse_settings("ERR failed to prepare settings")


def test_the_version_line():
    assert console.parse_version("Launcher 2.9.1") == "2.9.1"
    assert console.parse_version("Launcher beta-60a40c8\r") == "beta-60a40c8"
    assert console.parse_version("ERR unknown command") is None


# ---- through a broker -----------------------------------------------------------------

@pytest.fixture
def rig(tmp_path):
    made = []

    def make(**kw):
        node = FakeLauncher(**kw)
        c = mb.Console("cyd", mb.path_opener(node.path), str(tmp_path / "cyd.log"))
        broker = mb.Broker(c, str(tmp_path / "run" / "cyd.sock"))
        t = threading.Thread(target=broker.run, daemon=True)
        t.start()
        made.append((node, broker, t))
        end = time.time() + 3
        while not c.is_open() and time.time() < end:
            time.sleep(0.01)
        return node, broker_client.Board("cyd", path=broker.sock_path, timeout=20)

    yield make
    for node, broker, t in made:
        broker.shutdown()
        t.join(timeout=2)
        node.close()


def test_a_board_in_launcher_is_read_as_it_is(rig):
    node, board = rig(apps=[("camstr", 0x100000)], boot="camstr")
    with board as b:
        assert console.enter(b) == ("beta-60a40c8", "running")
        f = console.facts(b, wui_password="launcher")
    assert f["version"] == "beta-60a40c8" and f["boot"] == "camstr"
    assert [a["label"] for a in f["apps"]] == ["camstr"] and f["manager"]["label"] == "test"
    assert f["settings"]["wifi"] == ["lab-iot"] and "_wui_password" not in f["settings"]
    assert f["wui_password_matches"] is True
    assert console.facts(board.__enter__(), wui_password="other")["wui_password_matches"] is False
    assert node.reboots == 0, "a board that was in Launcher is left there"


def test_a_board_in_its_app_is_reset_into_launcher_through_the_banner(rig, monkeypatch):
    node, board = rig(apps=[("camstr", 0x100000)], boot="camstr", window=1.5, tick=0.05)
    # no modem lines on a pty: the fake's power-on stands in for the broker's reset
    time.sleep(0.1)
    node.state = "app"
    with board as b:
        monkeypatch.setattr(board, "request", _reset_powers_on(board, node))
        v, how = console.enter(b, wait=5, poll=0.2)
        assert (v, how) == ("beta-60a40c8", "entered")
        assert node.seen.count("nav SelPress") == 1
        console.leave(b)
    assert node.state == "app" and node.reboots == 1, "the app is booted again"


def test_a_version_reply_inside_the_window_waits_for_the_banner(rig, monkeypatch):
    """A CYD's console answers `version` before its boot screen prints the first banner: taking
    that reply for the menu stopped the attending, the window went unanswered, the app booted."""
    monkeypatch.setattr(console, "WINDOW", 2.0)
    node, board = rig(apps=[("camstr", 0x100000)], boot="camstr", window=1.5, tick=0.05, banner_delay=0.5)
    time.sleep(0.1)
    node.state = "app"
    with board as b:
        monkeypatch.setattr(board, "request", _reset_powers_on(board, node))
        v, how = console.enter(b, wait=5, poll=0.2)
        assert (v, how) == ("beta-60a40c8", "entered")
        assert node.seen.count("nav SelPress") == 1
        assert node.state == "menu", "the window was answered: Launcher stays in its menu"


def test_no_launcher_within_the_wait_is_said_plainly(rig, monkeypatch):
    node, board = rig(window=0.0)
    node.state = "app"
    with board as b:
        monkeypatch.setattr(board, "request", _reset_powers_on(board, node, window=0.0))
        with pytest.raises(console.ConsoleError, match="no Launcher console answered within 1 s"):
            console.enter(b, wait=1, poll=0.3)


def test_a_port_that_cannot_reset_says_so_and_stops_attending(rig):
    node, board = rig()
    node.state = "app"
    with board as b:
        with pytest.raises(console.ConsoleError, match="cannot reset the board"):
            console.enter(b, wait=1)
        assert b.request(op="attend", dialect="launcher", ttl=0)["answered"] == 0


def test_settings_are_written_and_read_back(rig):
    node, board = rig()
    with board as b:
        console.set_settings(b, {"bootToApp": False, "bootTimer": 2})
        f = console.read(b)
    assert f["settings"]["boot_to_app"] is False and f["settings"]["boot_timer"] == 2
    assert node.settings["bootToApp"] is False


def _reset_powers_on(board, node, window=None):
    real = board.request

    def request(**req):
        if req.get("op") == "reset":
            if window is not None:
                node.window = window
            node.power_on()
            return {"ok": True, "port": node.path}
        return real(**req)
    return request


def test_a_release_without_the_version_command_is_still_launcher(rig):
    """A 2.9.1 console answers `version` with ERR unknown command: Launcher has the port, its
    version is not readable here."""
    node, board = rig(has_version=False, apps=[("camstr", 0x100000)], boot="camstr")
    with board as b:
        assert console.probe(b) == console.NO_VERSION
        assert console.enter(b) == (console.NO_VERSION, "running")
        f = console.facts(b)
    assert f["version"] == "unknown" and f["boot"] == "camstr"


# ---- a release's printouts, as a board printed them ----------------------------------------

FIXTURES = os.path.join(REPO, "tests", "fixtures", "launcher", "t-embed-2.9.1")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


def test_a_release_prints_hex_subtypes_and_kb_mb_sizes_and_is_read_whole():
    t = console.parse_partitions(fixture("partitions.txt"))
    assert t["manager"] == {"label": "app0", "subtype": "test", "offset": 0x10000, "size": 0x180000}
    assert t["apps"] == [{"label": "buddy0", "subtype": "ota_0", "offset": 0x4A0000, "size": 0x120000, "boot": True}]
    assert [(d["label"], d["subtype"]) for d in t["data"]] == [("coredump", "coredump"), ("spiffs", "spiffs")]
    assert t["free"] == [{"offset": 0x5C0000, "size": 0xA40000}]
    assert (t["flash_size"], t["free_total"], t["boot"]) == (16 << 20, 10496 << 10, "buddy0")


def test_a_releases_settings_are_read_with_the_board_s_own_entries_left_alone():
    s = console.parse_settings(fixture("settings_get.txt"))
    assert s["boot_to_app"] is True and s["dev_mode"] is False and s["wui_user"] == "admin"
    assert s["auto_connect"] is True and s["only_bins"] is True and s["wifi"] == []
    assert "boot_timer" not in s and "deep_sleep_to_launcher" not in s, "a release has neither"
    assert s["_wui_password"] == "example-only" and s["_wifi_passwords"] == {}
    assert not any(k.startswith("02:00") or k in ("FGCOLOR", "bright") for k in s), "the device's own settings stay its own"
    assert console.parse_version(fixture("version.txt")) == "2.9.1"


def test_sizes_as_a_release_and_as_the_betas_print_them():
    assert console.human_bytes("16MB") == 16 << 20 and console.human_bytes("10496KB") == 10496 << 10
    assert console.human_bytes("640B") == 640 and console.human_bytes("4Mb") == 4 << 20
    assert console.subtype_name("app", "0x20") == "test" and console.subtype_name("app", "0x00") == "factory"
    assert console.subtype_name("app", "0x11") == "ota_1" and console.subtype_name("data", "0x02") == "nvs"
    assert console.subtype_name("data", "spiffs") == "spiffs" and console.subtype_name("data", "0x7f") == "0x7f"


def test_launchers_per_board_entry_names_the_board_that_saved_the_settings():
    """Keyed by the eFuse MAC of the board that wrote config.conf: a file carried from another
    board carries that board's key (a config copied from a twin board), so this is
    never taken for the chip on the port."""
    s = console.parse_settings(fixture("settings_get.txt"))
    assert (s["_settings_mac"], s["_settings_board"]) == ("02:00:00:00:00:01", "Lilygo T-Embed CC1101")
    assert console.settings_owner({"bootToApp": True}) == (None, None)
    assert console.settings_owner({"AA:BB:CC:DD:EE:FF-Some Board": 1}) == ("aa:bb:cc:dd:ee:ff", "Some Board")
    assert "_settings_mac" not in console.public(s) and "_settings_board" not in console.public(s)


def test_read_lifts_the_settings_owner_beside_the_table(rig):
    node, board = rig(mac="02:00:00:00:00:07", name="Fake CYD")
    with board as b:
        got = console.read(b)
    assert (got["settings_mac"], got["settings_board"]) == ("02:00:00:00:00:07", "Fake CYD")
    assert "_settings_mac" not in console.public(got["settings"])


def test_a_cyd_on_the_release_is_read_whole_too():
    """The second board read: the 2.9.1 release on a Cheap Yellow Display (4 MB, one app)."""
    d = os.path.join(REPO, "tests", "fixtures", "launcher", "cyd-2432s028-2.9.1")
    with open(os.path.join(d, "partitions.txt")) as f:
        t = console.parse_partitions(f.read())
    assert t["manager"] == {"label": "test", "subtype": "test", "offset": 0x10000, "size": 0x150000}
    assert t["apps"] == [{"label": "camstr", "subtype": "ota_0", "offset": 0x160000, "size": 0x100000, "boot": True}]
    assert t["data"] == [] and t["free"] == [{"offset": 0x260000, "size": 0x1A0000}]
    assert (t["flash_size"], t["free_total"], t["boot"]) == (4 << 20, 1664 << 10, "camstr")
    with open(os.path.join(d, "settings_get.txt")) as f:
        s = console.parse_settings(f.read())
    assert s["wifi"] == ["example-net"] and s["_wifi_passwords"] == {"example-net": None}
    assert (s["_settings_mac"], s["_settings_board"]) == ("02:00:00:00:00:01", "CYD 2432S028")
    assert s["boot_to_app"] is True and "boot_timer" not in s
    with open(os.path.join(d, "version.txt")) as f:
        assert console.parse_version(f.read()) == "2.9.1", "the release answers version"
