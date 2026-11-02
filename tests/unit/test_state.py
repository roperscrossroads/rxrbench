import json
import os

import pytest

from ansible_collections.rxrbench.mesh.plugins.module_utils import state

FIX = json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "shell",
                                  "legacy-0.4.36.json")))


def test_a_heltec_member():
    f = state.legacy_facts(FIX["board2"])
    assert f == {
        "state_supported": False, "build": "9a4d0d25", "built": "1002-1037",
        "board": "heltec_wifi_lora32_v4", "image": "0.4.36+0", "class": 4,
        "node_id": "0x0a0b0c0d",
        "lora": {"modem_preset": "SHORT_TURBO", "live_preset": "SHORT_TURBO", "region": "US",
                 "tx_power": 2, "tx_power_is_region_default": False, "tx_enabled": True},
        "device": {"role": "CLIENT", "rebroadcast_mode": "ALL"},
    }


def test_a_xiao_member():
    f = state.legacy_facts(FIX["board7"])
    assert (f["class"], f["board"], f["node_id"]) == (1, "xiao_ble", "0x0e1e2e3e")
    assert f["device"]["rebroadcast_mode"] == "NONE"


def test_a_head_and_a_brain():
    assert state.legacy_facts(FIX["xiao1"])["class"] == 12
    assert state.legacy_facts(FIX["xiao1"])["lora"]["modem_preset"] == "MEDIUM_FAST"
    assert state.legacy_facts(FIX["board3"])["class"] == 6


def test_the_listener_stores_what_the_bench_forbids():
    # Real capture: the listener's stored radio config is LongFast at 14 dBm
    # with TX on. Only its scanner keeps it quiet. The facts must say so plainly.
    f = state.legacy_facts(FIX["board6"])["lora"]
    assert (f["modem_preset"], f["tx_power"], f["tx_enabled"]) == ("LONG_FAST", 14, True)


@pytest.mark.parametrize("cmd", state.LEGACY_COMMANDS)
def test_a_missing_command_output_is_an_error(cmd):
    out = dict(FIX["board2"])
    del out[cmd]
    with pytest.raises(state.StateError, match="missing output"):
        state.legacy_facts(out)


@pytest.mark.parametrize("cmd,line,what", [
    ("meshtastic version", "class:", "fleet class"),
    ("meshtastic status", "node:", "node id"),
    ("meshtastic lora", "tx power:", "TX power"),
    ("meshtastic lora", "tx enabled:", "TX enabled"),
    ("meshtastic preset", "preset ", "live preset"),
])
def test_a_truncated_read_is_an_error_not_a_default(cmd, line, what):
    out = dict(FIX["board2"])
    out[cmd] = "\n".join(ln for ln in out[cmd].splitlines() if not ln.startswith(line))
    with pytest.raises(state.StateError, match=what):
        state.legacy_facts(out)


def test_an_unknown_preset_name_is_an_error():
    out = dict(FIX["board2"])
    out["meshtastic lora"] = out["meshtastic lora"].replace("ShortTurbo", "WarpNine")
    with pytest.raises(state.StateError, match="WarpNine"):
        state.legacy_facts(out)


def test_preset_names_round_trip():
    assert all(state.PRESETS[state.PRESET_DISPLAY[v]] == v for v in state.PRESET_DISPLAY)


# ---- the machine-readable state line (firmware with `meshtastic state`) ----------------------

import binascii  # noqa: E402

J = ('{"v":1,"build":"a1b2c3d4","image":"0.4.38+0","class":4,"board":"heltec_wifi_lora32_v4",'
     '"feat":["brain","cluster","phoneapi"],"id":"0x1b2b3b4b",'
     '"lora":{"preset":6,"use_preset":1,"live":6,"region":1,"pwr":2,"tx":1},'
     '"dev":{"role":0,"rb":0},"own":{"l":"rig \\"B\\"","s":"rB"},'
     '"ch":[{"i":0,"r":1,"n":"ShortFast","h":112,"k":"s1","up":1,"dn":1},'
     '{"i":1,"r":2,"n":"MediumFast","h":31,"k":"s1","up":0,"dn":0},'
     '{"i":2,"r":2,"n":"cluster","h":224,"k":"c:9f8e7d6c","up":0,"dn":0}],'
     '"brain":{"max":4,"heads":[{"a":1,"node":"0x1c2c3c4c","p":4,"w":6,"up":1},'
     '{"a":2,"node":"0x00000001","p":-1,"up":0}]},'
     '"scan":{"shut":0,"sweep":0,"presets":[4,6]},'
     '"cluster":{"ch":2,"n":1,"hash":"2b93a530"},"managed":0}')


def framed(text):
    return f"~S{text}*{binascii.crc_hqx(text.encode(), 0):04x}"


def test_the_state_line_becomes_the_same_facts_shape():
    f = state.state_facts("uart:~$ meshtastic state\n" + framed(J) + "\n")
    assert f["state_supported"] is True
    assert (f["build"], f["image"], f["class"], f["node_id"]) == \
        ("a1b2c3d4", "0.4.38+0", 4, "0x1b2b3b4b")
    assert f["lora"] == {"modem_preset": "SHORT_FAST", "use_preset": True,
                         "live_preset": "SHORT_FAST", "region": "US", "tx_power": 2,
                         "tx_power_is_region_default": False, "tx_enabled": True}
    assert f["device"] == {"role": "CLIENT", "rebroadcast_mode": "ALL"}
    assert f["owner"] == {"long_name": 'rig "B"', "short_name": "rB"}
    assert f["channels"][0] == {"index": 0, "name": "ShortFast", "hash": 112, "role": "PRIMARY",
                                "uplink_enabled": True, "downlink_enabled": True,
                                "psk": "default", "expanded": False}
    assert f["channels"][2]["psk"] == "secret" and f["channels"][2]["fingerprint"] == "9f8e7d6c"
    assert f["brain"]["heads"] == [
        {"attachment": 1, "node_id": "0x1c2c3c4c", "link_up": True, "preset": "MEDIUM_FAST",
         "wanted_preset": "SHORT_FAST"},
        # the second head is as firmware before 0.4.39 reports it: no "w"
        {"attachment": 2, "node_id": "0x00000001", "link_up": False, "preset": None,
         "wanted_preset": None}]
    assert f["scanner"]["presets"] == ["MEDIUM_FAST", "SHORT_FAST"]
    assert f["cluster"] == {"channel_index": 2, "entries": 1, "doc_hash": "2b93a530"}
    assert f["managed"] is False


def test_a_log_line_around_the_state_line_does_not_matter():
    noisy = ("[00:00:01.000,000] <inf> meshtastic: RX from 0x1\nuart:~$ " + framed(J) +
             "\n[00:00:01.100,000] <inf> meshtastic: TX done\n")
    assert state.state_facts(noisy)["class"] == 4


def test_old_firmware_has_no_state_line():
    assert state.state_facts("state: command not found") is None


def test_a_damaged_state_line_is_an_error_not_old_firmware():
    good = framed(J)
    with pytest.raises(state.StateError, match="checksum"):
        state.state_facts(good.replace('"pwr":2', '"pwr":9'))
    with pytest.raises(state.StateError, match="incomplete"):
        state.state_facts(good[:200])
    with pytest.raises(state.StateError, match="incomplete"):
        state.state_facts(good[:-5])


def test_another_state_version_is_refused():
    with pytest.raises(state.StateError, match="state version 2"):
        state.state_facts(framed(J.replace('{"v":1,', '{"v":2,')))


def test_an_unknown_enum_value_is_named_as_unknown():
    f = state.state_facts(framed(J.replace('"preset":6', '"preset":99')))
    assert f["lora"]["modem_preset"] == "PRESET_99"


def test_the_expanded_default_key_is_recognised_by_its_fingerprint():
    # Captured on 0.4.38: a primary stores the default
    # key as its 16 bytes, and the firmware printed this fingerprint for it.
    assert state.key_fingerprint(state.DEFAULT_PSK) == "d30b87d9"
    assert state.key_kind("c:d30b87d9") == {"psk": "default", "expanded": True}
    assert state.key_kind("c:cea0a9a2") == {"psk": "secret", "fingerprint": "cea0a9a2"}
    assert state.key_kind("s3") == {"psk": "default3", "expanded": False}
    assert state.key_kind("none") == {"psk": "none"}


def test_a_damaged_state_line_is_read_again(monkeypatch):
    seen = []

    def facts(text):
        seen.append(text)
        if text == "cut":
            raise state.StateError("the state line is incomplete")
        return {"build": text}
    monkeypatch.setattr(state, "state_facts", facts)
    answers = iter(["cut", "abc12345"])

    class Board:
        def exec(self, cmd, deadline=None):
            return next(answers)
    assert state.read(Board()) == {"build": "abc12345"} and seen == ["cut", "abc12345"]
    always = Board()
    always.exec = lambda cmd, deadline=None: "cut"
    with pytest.raises(state.StateError):
        state.read(always)
