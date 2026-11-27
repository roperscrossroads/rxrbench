"""The survey's firmware probes are the families': what is kept of a state line, the fallback
for firmware without one, and which devices a family's probe is tried on."""
import importlib
import importlib.util
import os

import pytest

from ansible_collections.rxrbench.core.plugins.plugin_utils import families

us = importlib.import_module("ansible_collections.rxrbench.core.plugins.modules.usb_survey")


def spec(family):
    return families.specs()[family]


@pytest.mark.plugins
def test_a_state_line_is_kept_as_the_familys_survey_says():
    line = {"v": 1, "id": "0x0a0b0c0d", "board": "xiao_ble", "class": 12, "build": "abc",
            "image": "0.4.46+45", "feat": ["phoneapi"], "brain": {"max": 4, "heads": []}, "lora": {}}
    kept = us.summarize(line, spec("zephyrtastic")["survey"])
    assert kept == {"node_id": "0x0a0b0c0d", "board": "xiao_ble", "class": 12, "build": "abc",
                    "image": "0.4.46+45", "features": ["phoneapi"], "brain": True}
    assert us.summarize(None, spec("zephyrtastic")["survey"]) is None


def test_the_detected_front_end_is_kept_from_inside_the_lora_object():
    line = {"v": 1, "id": "0x0a0b0c0d", "board": "heltec_wifi_lora32_v4", "lora": {"fem": "gc1109"}}
    assert us.summarize(line, spec("zephyrtastic")["survey"])["fem"] == "gc1109"
    # firmware from before the field: nothing kept, not an error
    assert "fem" not in us.summarize({"id": "0x1", "lora": {"chip": 1}}, spec("zephyrtastic")["survey"])


@pytest.mark.plugins
def test_firmware_from_before_the_state_line_is_read_by_the_familys_fallback(monkeypatch):
    answers = {"meshtastic state": "uart:~$ meshtastic state\r\nstate: command not found\r\n",
               "meshtastic version": "build:  9aea7c0a (built 1002 UTC)\nboard:  xiao_ble\n"
                                     "image:  0.4.39+0 (MCUboot header)\nclass:  1\nuart:~$ "}
    monkeypatch.setattr(us, "holders", lambda tty: [])
    monkeypatch.setattr(us, "console_exchange", lambda tty, cmd, p, until=None, prompt=True: answers[cmd])
    probe, via = us.probe_console(None, "/dev/ttyX", {}, "zephyrtastic", spec("zephyrtastic"))
    assert via == "port"
    assert probe == {"build": "9aea7c0a", "board": "xiao_ble", "image": "0.4.39+0", "class": 1,
                     "no_state_line": True}


@pytest.mark.plugins
def test_a_family_says_which_devices_its_probe_is_tried_on():
    zt, stock = spec("zephyrtastic")["survey"], spec("meshtastic")["survey"]
    assert "zephyr-cdc" in zt["kinds"] and "tool" not in zt
    assert stock["tool"] == "probe_meshtastic.py" and stock["modes"] == ["stock-app"]
    assert spec("linux_rauc")["survey"] is None


STOCK_MESHCORE = "ver\r\n  -> v1.17.1-d929643 (Build: 14-Aug-2026)\r\n"


@pytest.mark.plugins
def test_stock_meshcore_is_recognised_by_its_ver_answer_on_the_port(monkeypatch):
    """A board on stock MeshCore is asked as stock MeshCore too, not only as stock Meshtastic."""
    asked = []

    def exchange(tty, cmd, p, until=None, prompt=True):
        asked.append((cmd, until, prompt))
        return STOCK_MESHCORE if cmd == "ver" else ""
    monkeypatch.setattr(us, "holders", lambda tty: [])
    monkeypatch.setattr(us, "console_exchange", exchange)
    probe, via = us.probe_console(None, "/dev/ttyX", {}, "meshcore", spec("meshcore"))
    assert via == "port"
    assert probe == {"version": "v1.17.1", "build": "d929643", "no_state_line": True}
    # `ver` once (no state line, no shell prompt), then its role (here it says none)
    assert asked == [("ver", "(Build:", False), ("get role", "\n  -> ", False)], asked
    fam = families.plugin("meshcore")
    assert fam.survey_tags(probe) == {"fw:meshcore", "radio:lora"}
    assert fam.survey_describe(probe) == ("stock MeshCore v1.17.1 (d929643), answering its "
                                          "text console")
    assert fam.enroll_note(probe) == {"delivered": "stock MeshCore v1.17.1-d929643"}


def _survey(monkeypatch, tmp_path, usb, answers):
    """survey() over one fake USB device with a serial port: which families were asked, in
    order, and what the record holds. `answers` {family: probe} for the console probes."""
    asked = []

    def console(name, tty, p, fam, spec_):
        asked.append(fam)
        return answers.get(fam), "port"

    def tool(tty, p, script):
        asked.append(script)
        raise RuntimeError("timed out")
    reg = tmp_path / "registry.yaml"
    reg.write_text("devices: {}\n")
    monkeypatch.setattr(us, "usb_devices", lambda: [usb])
    monkeypatch.setattr(us, "by_id", lambda d: {usb["syspath"]: [{"path": "/dev/ttyX", "id": "x"}]}
                        if "serial" in d else {})
    monkeypatch.setattr(us, "probe_console", console)
    monkeypatch.setattr(us, "probe_tool", tool)
    monkeypatch.setattr(us, "probe_info_uf2", lambda serial: None)
    p = {"registry": str(reg), "host": "h1", "only": None, "deep": False,
         "families": families.specs(), "boards": {}}
    return asked, us.survey(p)


T114 = {"usb_vid": "239a", "usb_pid": "8029", "serial": "0102030405060708",
        "syspath": "/sys/x/1-1", "busid": "1-1", "product": "HT-n5262", "manufacturer": "Heltec"}


@pytest.mark.plugins
def test_a_t114_on_stock_meshcore_is_named_by_its_console_and_not_asked_as_meshtastic(
        monkeypatch, tmp_path):
    asked, recs = _survey(monkeypatch, tmp_path, T114,
                          {"meshcore": {"version": "v1.17.1", "build": "d929643"}})
    assert asked == ["meshcore"], asked
    assert recs[0]["probes"]["meshcore"]["version"] == "v1.17.1"


@pytest.mark.plugins
def test_a_t114_that_answers_no_console_is_still_asked_as_stock_meshtastic(monkeypatch, tmp_path):
    asked, recs = _survey(monkeypatch, tmp_path, T114, {})
    # nothing on its console: a MeshCore companion's frames, then stock Meshtastic's phone API
    assert asked == ["meshcore", "probe_meshcore_companion.py", "probe_meshtastic.py"], asked


@pytest.mark.plugins
def test_a_board_with_a_state_line_is_not_asked_as_stock_meshcore(monkeypatch, tmp_path):
    """zephcore speaks stock MeshCore's CLI, `ver` included: once a family with a state line
    has answered, the lineless one is not asked."""
    esp = {"usb_vid": "303a", "usb_pid": "1001", "serial": "02:00:00:00:00:01",
           "syspath": "/sys/x/1-2", "busid": "1-2", "product": "USB JTAG/serial debug unit",
           "manufacturer": "Espressif"}
    asking = [f for f, s in families.specs().items()
              if "esp32-usb-jtag" in ((s.get("survey") or {}).get("kinds") or [])
              and not (s.get("identity") or {}).get("lineless")]
    assert asking, "some family with a state line asks an ESP32's native USB"
    asked, recs = _survey(monkeypatch, tmp_path, esp, {asking[0]: {"build": "abc"}})
    assert "meshcore" not in asked and asking[0] in recs[0]["probes"], asked


def test_a_board_that_does_not_answer_as_stock_meshcore_is_not_reported_as_failing_it(
        monkeypatch, tmp_path):
    """A board in an app with no console is not reported as "meshcore probe failed: no echo"."""
    reg = tmp_path / "registry.yaml"
    reg.write_text("devices: {}\n")

    def console(name, tty, p, fam, spec_):
        raise RuntimeError(f"x: `{spec_['identity']['command']}`: no echo")
    monkeypatch.setattr(us, "usb_devices", lambda: [T114])
    monkeypatch.setattr(us, "by_id", lambda d: {T114["syspath"]: [{"path": "/dev/ttyX", "id": "x"}]}
                        if "serial" in d else {})
    monkeypatch.setattr(us, "probe_console", console)
    monkeypatch.setattr(us, "probe_tool", lambda tty, p, script: {})
    monkeypatch.setattr(us, "probe_info_uf2", lambda serial: None)
    recs = us.survey({"registry": str(reg), "host": "h1", "only": None, "deep": False,
                      "families": families.specs(), "boards": {}})
    assert "meshcore" not in recs[0]["probe_errors"], recs[0]["probe_errors"]


def _companion_tool():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections",
                        "rxrbench", "mesh", "host", "bin", "probe_meshcore_companion.py")
    spec_ = importlib.util.spec_from_file_location("probe_meshcore_companion", path)
    m = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(m)
    return m


def _device_info(version="v1.17.1-d929643", model="Heltec T114"):
    def pad(t, n):
        return t.encode().ljust(n, b"\0")
    return (bytes([13, 8, 175, 40]) + b"\x01\x02\x03\x04" + pad("14-Aug-2026", 12) + pad(model, 40)
            + pad(version, 20) + bytes([0, 0]))


@pytest.mark.plugins
def test_a_companion_is_read_from_its_device_info_frame_among_other_output():
    """A companion build speaks frames on USB, not the text CLI: one device query, one frame
    back, whatever else the port printed around it."""
    m = _companion_tool()
    info = _device_info()

    class Port:
        def __init__(self):
            self.sent, self.out = b"", b"boot noise\r\n" + b">" + len(info).to_bytes(2, "little")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def reset_input_buffer(self):
            pass

        def write(self, data):
            self.sent += data

        def read(self, n):
            if self.out:
                chunk, self.out = self.out, b""
                return chunk
            if self.sent and info:
                return info
            return b""
    p = Port()
    got = m.probe("/dev/ttyX", timeout=1.0, serial_open=lambda path: p)
    assert p.sent == b"<\x02\x00\x16\x03", "a CMD_DEVICE_QUERY frame, asking protocol 3"
    assert got == {"role": "companion", "fw_ver_code": 8, "build_date": "14-Aug-2026",
                   "model": "Heltec T114", "version": "v1.17.1", "build": "d929643"}
    assert m.device_info(b"\x05" + b"\0" * 90) is None
    fam = families.plugin("meshcore")
    assert "role:companion" in fam.survey_tags(got)
    assert fam.survey_describe(got).startswith("stock MeshCore companion v1.17.1 (d929643)")


@pytest.mark.plugins
def test_a_text_console_node_is_asked_its_role_once_it_answered(monkeypatch):
    answers = {"ver": STOCK_MESHCORE, "get role": "get role\r\n  -> > repeater\r\n"}
    monkeypatch.setattr(us, "holders", lambda tty: [])
    monkeypatch.setattr(us, "console_exchange",
                        lambda tty, cmd, p, until=None, prompt=True: answers.get(cmd, ""))
    probe, _ = us.probe_console(None, "/dev/ttyX", {}, "meshcore", spec("meshcore"))
    assert probe == {"version": "v1.17.1", "build": "d929643", "role": "repeater", "no_state_line": True}


@pytest.mark.plugins
def test_a_zephcore_board_is_named_by_its_state_line_not_as_stock_meshcore(monkeypatch, tmp_path):
    """A board on zephcore, which answers `ver` in stock MeshCore's words, is surveyed as zephcore,
    not as stock MeshCore."""
    esp = {"usb_vid": "303a", "usb_pid": "1001", "serial": "02:00:00:00:00:01",
           "syspath": "/sys/x/1-2", "busid": "1-2", "product": "USB JTAG/serial debug unit",
           "manufacturer": "Espressif"}
    zc = {"version": "0.1.0", "build": "a7954b2e", "role": "repeater", "board": "heltec_wifi_lora32_v4"}
    asked, recs = _survey(monkeypatch, tmp_path, esp, {"zephcore": zc, "meshcore": {"version": "v1.17.1"}})
    assert "zephcore" in asked and "meshcore" not in asked, asked
    assert set(recs[0]["probes"]) == {"zephcore"}
    assert families.plugin("zephcore").survey_tags(zc) >= {"fw:zephcore", "role:repeater"}


def test_one_entry_on_two_bridges_is_neither_and_the_hosts_mouse_is_left_out(monkeypatch, tmp_path):
    """cyd1 (a CH340, VID:PID only) and a second CH340 board are filed apart, and the host's mouse
    is not offered for enrollment."""
    reg = tmp_path / "registry.yaml"
    reg.write_text("devices:\n  cyd1: {kind: serial, match: {usb_vid: '1a86', usb_pid: '7523'}}\n")
    a = {"usb_vid": "1a86", "usb_pid": "7523", "busid": "1-1.3", "syspath": "/sys/x/1-1.3",
         "interfaces": ["ff/01/02"]}
    b = {**a, "busid": "1-1.4.1", "syspath": "/sys/x/1-1.4/1-1.4.1"}
    mouse = {"usb_vid": "093a", "usb_pid": "2510", "busid": "1-1.1", "syspath": "/sys/x/1-1.1",
             "interfaces": ["03/01/02"]}
    monkeypatch.setattr(us, "usb_devices", lambda: [a, b, mouse])
    monkeypatch.setattr(us, "by_id", lambda d: {})
    monkeypatch.setattr(us, "probe_info_uf2", lambda serial: None)
    recs = us.survey({"registry": str(reg), "host": "h1", "only": None, "deep": False,
                      "families": families.specs(), "boards": {}})
    assert [r["usb"]["busid"] for r in recs] == ["1-1.3", "1-1.4.1"], "the mouse is left out"
    assert all(r["registered"] == [] for r in recs)
    assert all(r["class"]["ambiguous"] == {"cyd1": ["1-1.3", "1-1.4.1"]} for r in recs)
    assert len({r["key"] for r in recs}) == 2 and "cyd1" not in {r["key"] for r in recs}


STLINK = {"usb_vid": "0483", "usb_pid": "3748", "serial": "ST01", "syspath": "/sys/x/1-3",
          "busid": "1-3", "product": "STM32 STLink", "manufacturer": "STMicroelectronics"}
FIXTURE_BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "tests", "fixtures", "collections", "ansible_collections", "fixture",
                           "widgets", "host", "bin")


def _probe_survey(monkeypatch, tmp_path, registry):
    """survey() over one ST-Link (no serial port), with the fixture plugin's probechip family's
    survey and its real tool."""
    from ansible_collections.rxrbench.core.plugins.plugin_utils import registry as plugins
    roots = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "tests", "fixtures", "collections")
    bench = plugins.load(["fixture.widgets"], roots=[roots])["fixture.widgets"]
    reg = tmp_path / "registry.yaml"
    reg.write_text(registry)
    monkeypatch.setattr(us, "usb_devices", lambda: [STLINK])
    monkeypatch.setattr(us, "by_id", lambda d: {})
    monkeypatch.setattr(us, "probe_info_uf2", lambda serial: None)
    fams = {"probechip": {"identity": None, "survey": bench.ProbeChip.survey}}
    p = {"registry": str(reg), "host": "h1", "only": None, "deep": False, "families": fams,
         "boards": {}, "plugin_bin": FIXTURE_BIN, "timeout": 30}
    return us.survey(p)


REGISTRY = """devices:
  probe1: {kind: usb, match: {usb_vid: "0483", usb_pid: "3748"}}
  mcu1:
    kind: swd
    reach: {swd: {host: h1, probe: probe1, chip: STM32F103C8, uid: "0123", nrst: true}}
  mcu2:
    kind: swd
    reach: {swd: {host: h1, probe: other, chip: STM32F103C8, uid: "4567"}}
"""


def test_a_chip_behind_a_debug_probe_is_asked_by_its_familys_tool(monkeypatch, tmp_path):
    recs = _probe_survey(monkeypatch, tmp_path, REGISTRY)
    assert recs[0]["registered"] == ["probe1"]
    assert recs[0]["probes"]["probechip"] == {"probe": "probe1",
                                              "chips": [{"name": "mcu1", "uid": "0123"}]}
    assert recs[0]["probe_errors"] == {}


def test_an_unregistered_probe_is_not_asked(monkeypatch, tmp_path):
    recs = _probe_survey(monkeypatch, tmp_path, "devices: {}\n")
    assert "probechip" not in recs[0]["probes"] and recs[0]["probe_errors"] == {}


# ---- a board with a debug probe built in (a Nucleo's ST-Link V2-1) -------------------------

from ansible_collections.rxrbench.core.plugins.module_utils import hwsurvey  # noqa: E402

ONBOARD = {"usb_vid": "0483", "usb_pid": "374b", "serial": "0671FF485570854967081043",
           "syspath": "/sys/x/1-1.2.4", "busid": "1-1.2.4", "product": "STM32 STLink",
           "manufacturer": "STMicroelectronics"}
NUCLEO = {"nucleo-f767zi": {"mcu": "stm32f767", "probe_drive": "NOD_F767ZI"}}
DRIVE = {"label": "NOD_F767ZI", "firmware": "V2J39M27", "mbed_code": "08180221003863363F7CFA13",
         "mount": "/media/x/NOD_F767ZI"}
SWD_CHIP = """#!/usr/bin/env python3
import json, os, sys
open(os.environ["SWD_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")
print(json.dumps({"uid": "003a001a3034511930363235", "idcode": "0x10016451",
                  "flash_size": 2097152, "voltage": 3.24}))
"""


def test_a_built_in_probes_drive_says_the_board_and_its_firmware():
    got = hwsurvey.parse_probe_drive(
        "NOD_F767ZI", "Version: V2J39M27\r\nBuild:   Oct 22 2021 16:50:45\r\n",
        '<meta http-equiv="refresh" content="0; URL=http://mbed.org/device/?code=08180221003863363F7CFA13"/>')
    assert got == {"label": "NOD_F767ZI", "firmware": "V2J39M27", "build": "Oct 22 2021 16:50:45",
                   "mbed_code": "08180221003863363F7CFA13"}
    assert hwsurvey.board_of_drive("NOD_F767ZI", NUCLEO) == "nucleo-f767zi"
    assert hwsurvey.board_of_drive("NOD_L476RG", NUCLEO) is None


def _onboard_survey(monkeypatch, tmp_path, registry):
    reg = tmp_path / "registry.yaml"
    reg.write_text(registry)
    (tmp_path / "swd_chip.py").write_text(SWD_CHIP)
    monkeypatch.setenv("SWD_LOG", str(tmp_path / "log"))
    monkeypatch.setattr(us, "usb_devices", lambda: [ONBOARD])
    monkeypatch.setattr(us, "by_id", lambda d: {})
    monkeypatch.setattr(us, "probe_drive", lambda serial: dict(DRIVE))
    p = {"registry": str(reg), "host": "h1", "only": None, "deep": False, "families": {},
         "boards": NUCLEO, "plugin_bin": str(tmp_path), "timeout": 30}
    recs = us.survey(p)
    log = (tmp_path / "log").read_text() if (tmp_path / "log").exists() else ""
    return recs, log


def test_an_unregistered_built_in_probes_chip_is_read_without_halting_it(monkeypatch, tmp_path):
    recs, log = _onboard_survey(monkeypatch, tmp_path, "devices: {}\n")
    probes = recs[0]["probes"]
    assert probes["probe_drive"]["label"] == "NOD_F767ZI"
    assert probes["swd"]["board_type"] == "nucleo-f767zi"
    assert probes["swd"]["uid"] == "003a001a3034511930363235"
    assert "id --look --chip stm32f767 --probe stlink --location 1-1.2.4" in log
    assert {"probe-drive:NOD_F767ZI", "board-type:nucleo-f767zi",
            "probe-firmware:V2J39M27"} <= set(recs[0]["tags"])


def test_a_registered_probes_chips_are_left_to_the_registry_and_its_family(monkeypatch, tmp_path):
    reg = ('devices:\n  stlink-n: {kind: usb, match: {usb_vid: "0483", usb_pid: "374b", '
           'usb_serial: "0671FF485570854967081043"}}\n')
    recs, log = _onboard_survey(monkeypatch, tmp_path, reg)
    assert recs[0]["registered"] == ["stlink-n"]
    assert "swd" not in recs[0]["probes"] and log == ""
    assert recs[0]["probes"]["probe_drive"]["firmware"] == "V2J39M27"


# ---- what a flasher holds is neither asked nor filed (busy marks) -------------------------

def _held_survey(monkeypatch, tmp_path, usb, marks, registry="devices: {}\n"):
    import json
    import time
    state = tmp_path / "state"
    (state / "busy").mkdir(parents=True)
    for name, m in marks.items():
        (state / "busy" / f"{name}.json").write_text(json.dumps({"until": time.time() + 60, **m}))
    monkeypatch.setenv("RXRBENCH_HOST_STATE", str(state))
    asked = []
    reg = tmp_path / "registry.yaml"
    reg.write_text(registry)
    monkeypatch.setattr(us, "usb_devices", lambda: [usb])
    monkeypatch.setattr(us, "by_id", lambda d: {usb["syspath"]: [{"path": "/dev/ttyX", "id": "x"}]}
                        if "serial" in d else {})
    monkeypatch.setattr(us, "probe_console", lambda *a, **k: asked.append("console") or (None, None))
    monkeypatch.setattr(us, "probe_tool", lambda *a, **k: asked.append("tool"))
    monkeypatch.setattr(us, "probe_info_uf2", lambda serial: asked.append("uf2"))
    monkeypatch.setattr(us, "rp2040_rom_owner", lambda *a, **k: asked.append("picotool") or (None, None))
    p = {"registry": str(reg), "host": "h1", "only": None, "deep": False,
         "families": families.specs(), "boards": {}}
    return asked, us.survey(p), p


@pytest.mark.parametrize("mark", [{"busids": ["1-1"], "keys": []},
                                  {"busids": [], "keys": ["0102030405060708"]}])
def test_a_device_a_flasher_holds_is_neither_asked_nor_filed(monkeypatch, tmp_path, mark):
    asked, recs, p = _held_survey(monkeypatch, tmp_path, T114, {"t114": mark})
    assert asked == [] and recs == [] and p["held"] == ["1-1"]


def test_an_rp2040_rom_is_held_back_while_any_registered_rp2040_is_flashed(monkeypatch, tmp_path):
    rom = {"usb_vid": "2e8a", "usb_pid": "0003", "serial": "A1A2A3A4A5A6", "syspath": "/sys/x/1-2",
           "busid": "1-2", "product": "RP2 Boot", "manufacturer": "Raspberry Pi"}
    reg = 'devices:\n  pog9: {match: {usb_serial: "0A0B0C0D0E0F1011"}}\n'
    asked, recs, p = _held_survey(monkeypatch, tmp_path, rom, {"pog9": {"keys": ["0a0b0c0d0e0f1011"]}}, reg)
    assert "picotool" not in asked and recs == [] and p["held"] == ["1-2"]


def test_a_lapsed_mark_holds_nothing(monkeypatch, tmp_path):
    asked, recs, p = _held_survey(monkeypatch, tmp_path, T114,
                                  {"t114": {"busids": ["1-1"], "until": 1}})
    assert recs and p["held"] == []
