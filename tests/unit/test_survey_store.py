import importlib.util
import os

spec = importlib.util.spec_from_file_location(
    "survey_store", os.path.join(os.path.dirname(__file__), "..", "..", "tools", "survey_store.py"))
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)

USB = {"usb_vid": "2fe3", "usb_pid": "0004", "serial": "0A0B0C0D0E0F1011", "busid": "1-1.2"}


def rec(probes):
    return {"key": "board8", "usb": USB, "class": {"kind": "zephyr-cdc", "mode": "app"},
            "registered": ["board8"], "probes": probes}


def test_a_first_flash_moves_the_stock_record_to_history(widgets):
    old = store.merge(None, rec({"gadget": {"version": "2.6.2", "model": "X"}}), "bench-a", "t1")
    assert "fw:gadget" in old["tags"]
    new = store.merge(old, rec({"widget": {"class": 1, "board": "xiao_ble"}}), "bench-a", "t2")
    assert "gadget" not in new["probes"] and new["history"]["gadget"]["version"] == "2.6.2"
    assert "fw:gadget" not in new["tags"] and "fw:widget" in new["tags"]


def test_a_probe_that_did_not_run_this_time_is_kept():
    old = store.merge(None, rec({"bootloader": {"version": "0.9.2-OTAFIX2.5"}}), "bench-a", "t1")
    new = store.merge(old, rec({}), "bench-a", "t2")
    assert new["probes"]["bootloader"]["version"] == "0.9.2-OTAFIX2.5"


def test_a_board_seen_before_it_was_registered_is_filed_under_its_name(tmp_path, monkeypatch, capsys, widgets):
    import json
    import sys
    import yaml
    early = {"key": "new-239a810b-0A0B0C0D0E0F1011", "first_seen": "t0",
             "probes": {"gadget": {"version": "2.6.2"}}}
    (tmp_path / "new-239a810b-0A0B0C0D0E0F1011.yaml").write_text(yaml.safe_dump(early))
    r = {**rec({"widget": {"class": 1, "board": "xiao_ble"}}),
         "class": {"kind": "zephyr-cdc", "mode": "app", "ids": {"chip_id": "0A0B0C0D0E0F1011"}}}
    monkeypatch.setattr(sys, "argv", ["x", "--host", "bench-a", "--facts-dir", str(tmp_path)])
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps([r])))
    store.main()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["board8.yaml"]
    merged = yaml.safe_load((tmp_path / "board8.yaml").read_text())
    assert merged["first_seen"] == "t0" and merged["history"]["gadget"]["version"] == "2.6.2"


def test_a_face_of_a_board_its_entry_lacks_is_said_with_the_entry_to_add():
    rec = {"class": {"mcu": "rp2040", "mode": "bootloader", "ids": {"chip_id": "0A0B0C0D0E0F1011"},
                     "unregistered_face": {"usb_vid": "2e8a", "usb_pid": "0003", "usb_serial": "0A1B2C3D4E5F"}},
           "probes": {}}
    line = store.describe(rec)
    assert "chip_id=0A0B0C0D0E0F1011" in line
    assert "match_also: [{usb_vid: '2e8a', usb_pid: '0003', usb_serial: '0A1B2C3D4E5F'}]" in line


def test_a_built_in_probes_board_type_survives_into_the_stored_tags():
    """The store recomputes the tags after merging; without the board types it dropped the one a
    probe's drive label gives (seen on the Pi: NOD_F767ZI with no board-type tag)."""
    r = {"key": "new-0483374b", "usb": {"usb_vid": "0483", "usb_pid": "374b"},
         "class": {"kind": "swd-probe", "probe": "stlink-v2-1", "mode": "probe"},
         "registered": [], "serial_ports": [], "block": [],
         "probes": {"probe_drive": {"label": "NOD_F767ZI", "firmware": "V2J39M27"}}}
    boards = {"nucleo-f767zi": {"mcu": "stm32f767", "probe_drive": "NOD_F767ZI"}}
    new = store.merge(None, r, "pi1", "t1", boards)
    assert "board-type:nucleo-f767zi" in new["tags"] and "probe-drive:NOD_F767ZI" in new["tags"]
