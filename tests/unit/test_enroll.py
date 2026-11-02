"""enroll writes one registry entry from survey facts and changes nothing else."""
import importlib.util
import os
import subprocess
import sys

import pytest
import yaml

TOOL = os.path.join(os.path.dirname(__file__), "..", "..", "tools", "enroll.py")
REGISTRY = """version: 1
# a comment that must survive
hosts:
  bench-a: {}            # no ssh: nothing is deployed in a test
devices:
  board7:
    kind: serial
    aliases: [b131]
    match:
      usb_serial: "2A2B2C2D2E2F3031"   # keep this comment
protected:
  bench-a: [/dev/sda2]
"""
FACTS = {"key": "new-239a810b-1A1B1C1D1E1F2021", "host": "bench-a", "last_seen": "2026-10-03T14:09:22Z",
         "usb": {"manufacturer": "Seeed Studio", "product": "XIAO-BOOT"},
         "class": {"kind": "xiao-nrf52840", "mcu": "nrf52840", "mode": "stock-app",
                   "ids": {"chip_id": "1A1B1C1D1E1F2021"}},
         "registered": [], "serial_ports": ["usb-Seeed_Studio_XIAO-BOOT_1A1B1C1D1E1F2021-if00"],
         "probes": {"meshtastic": {"node_id": "0x0f1f2f3f", "short_name": "2f3f",
                                   "firmware_version": "2.6.2", "hw_model": "XIAO_NRF52_KIT"}},
         "tags": ["mcu:nrf52840", "fw:stock-meshtastic"]}


@pytest.fixture
def site(tmp_path):
    (tmp_path / "registry.yaml").write_text(REGISTRY)
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / (FACTS["key"] + ".yaml")).write_text(yaml.safe_dump(FACTS))
    # HOME too: enroll refreshes ~/.devreg/registry.yaml when this machine is a bench host
    env = {**os.environ, "DEVREG_REGISTRY": str(tmp_path / "registry.yaml"),
           "RXRBENCH_FACTS": str(tmp_path / "facts"), "HOME": str(tmp_path)}
    return tmp_path, env


def run(env, *args):
    return subprocess.run([sys.executable, TOOL, *args], env=env, capture_output=True, text=True)


FIXTURE_COLLECTIONS = os.path.join(os.path.dirname(__file__), "..", "fixtures", "collections")
#: a kit the survey found running the fixture plugin's gadget release
GADGET_FACTS = {**FACTS, "probes": {"gadget": {"version": "2.6.2", "model": "GADGET_KIT", "name": "2f3f"}},
                "tags": ["mcu:nrf52840", "fw:gadget"]}


@pytest.fixture
def fixture_site(site):
    """A site of its own whose plugin is the fixture plugin (under `<site>/collections`), with
    the test board types in its catalog and an empty layout `lay1`; its facts are a gadget's."""
    import shutil
    tmp, env = site
    root = tmp / "site"
    (root / "layouts" / "lay1").mkdir(parents=True)
    (root / "layouts" / "lay1" / "devreg.yml").write_text(
        "plugin: rxrbench.core.devreg\nregistry: ../../registry.yaml\nboards: {}\n")
    (root / "catalog").mkdir()
    shutil.copy(os.path.join(os.path.dirname(__file__), "..", "fixtures", "catalog", "boards.yml"),
                root / "catalog" / "boards.yml")
    (root / "plugins.yml").write_text("plugins: [fixture.widgets]\n")
    os.symlink(os.path.abspath(FIXTURE_COLLECTIONS), root / "collections")
    (tmp / "facts" / (FACTS["key"] + ".yaml")).write_text(yaml.safe_dump(GADGET_FACTS))
    return tmp, {**env, "RXRBENCH_SITE": str(root)}, root


def test_a_new_kit_gets_one_entry_matched_by_chip_id_and_the_file_is_otherwise_untouched(fixture_site):
    tmp, env, _root = fixture_site
    r = run(env, "board9", "1A1B1C1D1E1F2021")
    assert r.returncode == 0, r.stderr
    text = (tmp / "registry.yaml").read_text()
    assert "# a comment that must survive" in text and "# keep this comment" in text
    reg = yaml.safe_load(text)
    e = reg["devices"]["board9"]
    assert e["match"] == {"usb_serial": "1A1B1C1D1E1F2021"} and e["aliases"] == ["2f3f"]
    assert e["reach"] == {"usb": {"host": "bench-a"}} and e["identity"]["version"] == "2.6.2"
    assert reg["protected"] == {"bench-a": ["/dev/sda2"]} and reg["devices"]["board7"]["aliases"] == ["b131"]


@pytest.mark.plugins
def test_stock_meshtastic_says_its_name_and_node_id_for_its_entry(site):
    """The mesh plugin's enroll_note for a kit that arrived on stock Meshtastic."""
    tmp, env = site
    r = run(env, "board9", "1A1B1C1D1E1F2021")
    assert r.returncode == 0, r.stderr
    e = yaml.safe_load((tmp / "registry.yaml").read_text())["devices"]["board9"]
    assert e["aliases"] == ["2f3f"] and e["identity"]["node_id"] == "0x0f1f2f3f"
    assert sorted(p.name for p in (tmp / "facts").iterdir()) == ["board9.yaml"]


@pytest.mark.parametrize("name, why", [("board7", "already a device name"), ("b131", "already a device name"),
                                       ("Bad_Name", "lower case")])
def test_a_taken_or_malformed_name_is_refused_and_nothing_changes(site, name, why):
    tmp, env = site
    r = run(env, name, "1A1B1C1D1E1F2021")
    assert r.returncode != 0 and why in r.stderr
    assert (tmp / "registry.yaml").read_text() == REGISTRY


def test_an_unknown_device_is_refused(site):
    _, env = site
    r = run(env, "board9", "FFFFFFFFFFFFFFFF")
    assert r.returncode != 0 and "0 facts files match" in r.stderr


def test_a_machine_that_is_its_own_bench_host_gets_the_registry_in_place(site):
    tmp, env = site
    (tmp / ".devreg").mkdir()
    (tmp / ".devreg" / "registry.yaml").write_text("version: 1\ndevices: {}\n")
    r = run(env, "board9", "1A1B1C1D1E1F2021")
    assert r.returncode == 0, r.stderr
    assert "registry -> this machine: ok" in r.stdout
    assert (tmp / ".devreg" / "registry.yaml").read_text() == (tmp / "registry.yaml").read_text()


# A board vendor's or a firmware's own VID:PID: the survey has no row, so no chip id; the
# unit's serial still names it.
UNKNOWN = {"key": "new-1b4f0026-bench-a-1-13-1B2B3B4B5B6B7B8B", "host": "bench-a",
           "last_seen": "2026-11-02T17:03:00Z",
           "usb": {"usb_vid": "1b4f", "usb_pid": "0026", "serial": "1B2B3B4B5B6B7B8B",
                   "manufacturer": "Pog", "product": "Pog Keyboard"},
           "class": {"kind": "unknown", "mode": None}, "registered": [],
           "serial_ports": ["usb-Pog_Pog_Keyboard_1B2B3B4B5B6B7B8B-if00"], "probes": {}, "tags": []}


@pytest.mark.parametrize("serial, ok", [("1B2B3B4B5B6B7B8B", True), ("0001", False),
                                        ("12345678", False)])
def test_an_unknown_kind_is_matched_on_its_own_unit_serial(site, serial, ok):
    tmp, env = site
    facts = {**UNKNOWN, "usb": {**UNKNOWN["usb"], "serial": serial},
             "key": f"new-1b4f0026-bench-a-1-13-{serial}"}
    (tmp / "facts" / (facts["key"] + ".yaml")).write_text(yaml.safe_dump(facts))
    r = run(env, "pog1", serial)
    if ok:
        assert r.returncode == 0, r.stderr
        e = yaml.safe_load((tmp / "registry.yaml").read_text())["devices"]["pog1"]
        assert e["match"] == {"usb_serial": serial} and e["kind"] == "serial"
        assert "kind unknown here" in (tmp / "registry.yaml").read_text()
    else:
        assert r.returncode != 0 and "no chip id, MAC or unit serial" in r.stderr
        assert (tmp / "registry.yaml").read_text() == REGISTRY


def test_the_first_device_goes_into_an_empty_devices_map(site):
    tmp, env = site
    (tmp / "registry.yaml").write_text("version: 1\nhosts:\n  bench-a: {}\ndevices: {}   # none yet\n")
    r = run(env, "board9", "1A1B1C1D1E1F2021")
    assert r.returncode == 0, r.stderr
    reg = yaml.safe_load((tmp / "registry.yaml").read_text())
    assert list(reg["devices"]) == ["board9"] and reg["hosts"] == {"bench-a": {}}


def test_a_board_enrolled_in_its_bootloader_is_a_serial_device(site):
    tmp, env = site
    facts = {"key": "new-2e8a000f-2C2D2E2F30313233", "host": "bench-a", "last_seen": "2026-11-02T20:41:00Z",
             "usb": {"usb_vid": "2e8a", "usb_pid": "000f", "serial": "2C2D2E2F30313233",
                     "manufacturer": "Raspberry Pi", "product": "RP2350 Boot"},
             "class": {"kind": "rp2-bootrom", "mcu": "rp2350", "mode": "bootloader",
                       "ids": {"chip_id": "2C2D2E2F30313233"}},
             "registered": [], "serial_ports": [], "probes": {}, "tags": ["mcu:rp2350"]}
    (tmp / "facts" / (facts["key"] + ".yaml")).write_text(yaml.safe_dump(facts))
    r = run(env, "pico2w", "2C2D2E2F30313233")
    assert r.returncode == 0, r.stderr
    e = yaml.safe_load((tmp / "registry.yaml").read_text())["devices"]["pico2w"]
    assert e["kind"] == "serial" and e["match"] == {"usb_serial": "2C2D2E2F30313233"}


_spec = importlib.util.spec_from_file_location("enroll", TOOL)
enroll = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(enroll)

BOARDS = {
    "kit-a": {"mcu": "nrf52840", "zephyr_boards": ["board_a"],
              "uf2": {"bootloader_usb": ["1234:0045"]}},
    "kit-a-sense": {"mcu": "nrf52840", "zephyr_boards": ["board_a_sense"]},
    "kit-b": {"mcu": "esp32s3", "zephyr_boards": ["board_b"]},
    "kit-b2": {"mcu": "esp32s3", "zephyr_boards": ["board_b"]},
}


def test_the_board_type_comes_from_what_the_firmware_said_it_was_built_for():
    rec = {"tags": ["board:board_a", "fw:x"], "usb": {"usb_vid": "2fe3", "usb_pid": "0004"}}
    assert enroll.board_types(rec, BOARDS) == ["kit-a"]


def test_a_board_in_its_bootloader_is_typed_by_the_bootloaders_usb_id():
    rec = {"tags": ["usb-mode:bootloader"], "usb": {"usb_vid": "1234", "usb_pid": "0045"}}
    assert enroll.board_types(rec, BOARDS) == ["kit-a"]


def test_a_stock_app_is_typed_by_the_kind_the_survey_named():
    """A board known by its USB ids alone (a T114 on stock MeshCore: no Zephyr board name) gets a type."""
    rec = {"tags": ["usb-mode:stock-app"], "class": {"kind": "kit-a"},
           "usb": {"usb_vid": "239a", "usb_pid": "8029"}}
    assert enroll.board_types(rec, BOARDS) == ["kit-a"]
    named = {"tags": ["board:board_b"], "class": {"kind": "kit-a"}, "usb": {}}
    assert enroll.board_types(named, BOARDS) == ["kit-b", "kit-b2"], "a board name decides"


def test_two_types_that_fit_are_both_offered_and_none_chosen():
    rec = {"tags": ["board:board_b"], "usb": {}}
    assert enroll.board_types(rec, BOARDS) == ["kit-b", "kit-b2"]
    assert enroll.board_types({"tags": [], "usb": {}}, BOARDS) == []


def test_an_enrolled_board_gets_the_board_type_its_firmware_named(fixture_site):
    """Seen on a fresh bench: the survey tagged board:<zephyr board> and enroll left the type
    out, so no image could be declared for the board until it was added by hand."""
    tmp, env, _root = fixture_site
    f = tmp / "facts" / "new-239a810b-1A1B1C1D1E1F2021.yaml"
    rec = yaml.safe_load(f.read_text())
    rec["probes"] = {"widget": {"board": "xiao_ble", "class": 3}}
    rec["tags"] = ["board:xiao_ble", "fw:widget"]
    f.write_text(yaml.safe_dump(rec))
    r = run(env, "board9", "1A1B1C1D1E1F2021")
    assert r.returncode == 0, r.stderr
    assert "board type xiao" in r.stdout, r.stdout
    assert yaml.safe_load((tmp / "registry.yaml").read_text())["devices"]["board9"]["policy"] == {"board_tag": "xiao"}


def test_a_layout_that_would_be_ignored_or_is_not_there_is_refused_before_anything_is_written(fixture_site):
    """--layout without --key used to register the board and add it to no layout, silently."""
    tmp, env, _root = fixture_site
    r = run(env, "board9", "1A1B1C1D1E1F2021", "--layout", "lay1", "--family", "widget")
    assert r.returncode != 0 and "only with --key" in r.stderr
    # a family with no key (stock firmware) is not asked for one
    r = run(env, "board9", "1A1B1C1D1E1F2021", "--layout", "lay1", "--family", "gadget")
    assert r.returncode != 0 and "not told apart by a key" in r.stderr
    r = run(env, "board9", "1A1B1C1D1E1F2021", "--layout", "no-such-layout", "--key", "1")
    assert r.returncode != 0 and "no layout 'no-such-layout'" in r.stderr
    assert (tmp / "registry.yaml").read_text() == REGISTRY
    assert sorted(p.name for p in (tmp / "facts").iterdir()) == [FACTS["key"] + ".yaml"]


def test_a_stock_board_goes_into_a_layout_by_its_catalogued_image(fixture_site):
    """`--layout` declares a family with no identity key (a stock release) with --image, and a
    board the survey found running that firmware needs no first flash."""
    tmp, env, root = fixture_site
    (root / "catalog" / "firmware.yml").write_text(
        "images:\n  gadget-test:\n    family: gadget\n    board_tags: [xiao]\n"
        "    flasher: uf2\n    source: {type: path, path: /nonexistent}\n")
    r = run(env, "board9", "1A1B1C1D1E1F2021", "--layout", "lay1", "--family", "gadget",
            "--image", "no-such-image")
    assert r.returncode != 0 and "no catalogued gadget image" in r.stderr, r.stderr
    assert (tmp / "registry.yaml").read_text() == REGISTRY, "refused before anything is written"
    r = run(env, "board9", "1A1B1C1D1E1F2021", "--layout", "lay1", "--family", "gadget",
            "--image", "gadget-test")
    assert r.returncode == 0, r.stderr
    hv = yaml.safe_load((root / "layouts" / "lay1" / "host_vars" / "board9.yml").read_text())
    assert hv["firmware"] == {"family": "gadget", "image": "gadget-test"}
    # how it is written is the catalog's (the image's flasher), which the layout takes: enroll
    # writes no flasher that would override it
    assert "bench_flasher" not in hv and "bench_first_flash" not in hv


def test_a_key_that_names_no_catalogued_image_is_refused_before_anything_is_written(fixture_site):
    tmp, env, root = fixture_site
    r = run(env, "board9", "1A1B1C1D1E1F2021", "--layout", "lay1", "--family", "widget", "--key", "3")
    assert r.returncode != 0 and "widget class 3" in r.stderr and "Nothing written" in r.stderr, r.stderr
    assert (tmp / "registry.yaml").read_text() == REGISTRY
    assert not (root / "layouts" / "lay1" / "host_vars").exists()


@pytest.mark.plugins
def test_a_class_is_checked_against_the_catalog_and_its_flasher_left_to_the_layout(site):
    """zephyrtastic's images are told apart by class: a class the catalog has is declared with
    no flasher line (the class's record says how it is written); one it lacks is refused."""
    tmp, env = site
    root = tmp / "site"
    (root / "layouts" / "lay1").mkdir(parents=True)
    (root / "layouts" / "lay1" / "devreg.yml").write_text(
        "plugin: rxrbench.core.devreg\nregistry: ../../registry.yaml\nboards: {}\n")
    (root / "plugins.yml").write_text("plugins: [rxrbench.mesh]\n")
    env = {**env, "RXRBENCH_SITE": str(root)}
    r = run(env, "board9", "1A1B1C1D1E1F2021", "--layout", "lay1", "--family", "zephyrtastic", "--key", "99")
    assert r.returncode != 0 and "class 99 is not in the catalog" in r.stderr, r.stderr
    assert (tmp / "registry.yaml").read_text() == REGISTRY
    r = run(env, "board9", "1A1B1C1D1E1F2021", "--layout", "lay1", "--family", "zephyrtastic", "--key", "1")
    assert r.returncode == 0, r.stderr
    hv = yaml.safe_load((root / "layouts" / "lay1" / "host_vars" / "board9.yml").read_text())
    assert hv["firmware"] == {"family": "zephyrtastic", "class": 1} and "bench_flasher" not in hv


def _bridge_facts(tmp, mac=True):
    """A board behind a CH340 (no USB serial of its own); the deep survey read its chip's MAC
    through the bridge, or (mac=False) only the passive survey ran."""
    rec = {"key": "new-1a867523-port-1-1.4", "host": "bench-a", "last_seen": "t1",
           "usb": {"usb_vid": "1a86", "usb_pid": "7523", "serial": None,
                   "manufacturer": "QinHeng", "product": "USB Serial"},
           "class": {"kind": "uart-bridge", "bridge": "ch340", "mode": "bridge", "ids": {}},
           "registered": [], "serial_ports": ["/dev/ttyUSB0"],
           "probes": {"esptool": {"mac": "02:00:00:00:00:24", "chip": "ESP32"}} if mac else {},
           "tags": []}
    for p in (tmp / "facts").iterdir():
        p.unlink()
    (tmp / "facts" / (rec["key"] + ".yaml")).write_text(yaml.safe_dump(rec))
    return rec["key"]


def test_a_board_behind_a_serial_less_bridge_is_enrolled_by_the_mac_the_deep_survey_read(site):
    tmp, env = site
    key = _bridge_facts(tmp)
    r = run(env, "cyd9", key)
    assert r.returncode == 0, r.stderr
    e = yaml.safe_load((tmp / "registry.yaml").read_text())["devices"]["cyd9"]
    assert e["match"] == {"usb_vid": "1a86", "usb_pid": "7523"}
    assert e["policy"]["chip_mac"] == "02:00:00:00:00:24", "every esptool write checks it first"
    assert "ambiguous" in e["notes"], "a second bridge of the same make cannot be told apart"


def test_a_serial_less_bridge_with_no_mac_read_is_sent_to_the_deep_survey(site):
    tmp, env = site
    key = _bridge_facts(tmp, mac=False)
    r = run(env, "cyd9", key)
    assert r.returncode != 0 and "run the deep survey" in r.stderr, r.stderr
    assert (tmp / "registry.yaml").read_text() == REGISTRY, "nothing written"


def test_a_board_behind_its_built_in_probe_gets_the_type_its_drive_names():
    spec = importlib.util.spec_from_file_location("enroll", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rec = {"class": {"kind": "swd-probe"}, "usb": {"usb_vid": "0483", "usb_pid": "374b"},
           "tags": ["probe-drive:NOD_F767ZI", "tool:swd-probe"]}
    boards = {"nucleo-f767zi": {"mcu": "stm32f767", "probe_drive": "NOD_F767ZI"},
              "bluepill-f103": {"mcu": "stm32f103"}}
    assert mod.board_types(rec, boards) == ["nucleo-f767zi"]


# ---- a board behind its built-in debug probe: two entries ------------------------------------

PROBE_FACTS = {
    "key": "new-0483374b-bench-a-1-2-0671FF485570854967081043", "host": "bench-a",
    "last_seen": "2026-11-10T17:47:04Z",
    "usb": {"usb_vid": "0483", "usb_pid": "374b", "serial": "0671FF485570854967081043",
            "product": "STM32 STLink", "manufacturer": "STMicroelectronics", "busid": "1-2"},
    "class": {"kind": "swd-probe", "probe": "stlink-v2-1", "mode": "probe"},
    "registered": [],
    "serial_ports": ["usb-STMicroelectronics_STM32_STLink_0671FF485570854967081043-if02"],
    "probes": {"probe_drive": {"label": "NOD_F767ZI", "firmware": "V2J39M27"},
               "swd": {"board_type": "nucleo-f767zi", "chip": "stm32f767",
                       "uid": "003a001a3034511930363235", "flash_size": 2097152}},
    "tags": ["board-type:nucleo-f767zi", "probe-drive:NOD_F767ZI", "tool:swd-probe"]}
NUCLEO_BOARDS = "boards:\n  nucleo-f767zi: {mcu: stm32f767, probe_drive: NOD_F767ZI}\n"


@pytest.fixture
def probe_site(site):
    tmp, env = site
    root = tmp / "site"
    (root / "catalog").mkdir(parents=True)
    (root / "catalog" / "boards.yml").write_text(NUCLEO_BOARDS)
    (root / "plugins.yml").write_text("plugins: []\n")
    for f in (tmp / "facts").iterdir():
        f.unlink()

    def facts(**change):
        rec = {**PROBE_FACTS, **change}
        (tmp / "facts" / (rec["key"] + ".yaml")).write_text(yaml.safe_dump(rec))
    facts()
    return tmp, {**env, "RXRBENCH_SITE": str(root)}, facts


@pytest.mark.parametrize("ident", ["0671FF485570854967081043", "003a001a3034511930363235"])
def test_a_board_behind_its_built_in_probe_is_enrolled_as_the_probe_and_the_chip(probe_site, ident):
    tmp, env, _facts = probe_site
    r = run(env, "nf7", ident)
    assert r.returncode == 0, r.stderr
    text = (tmp / "registry.yaml").read_text()
    assert "# a comment that must survive" in text and "# keep this comment" in text
    reg = yaml.safe_load(text)
    probe, board = reg["devices"]["stlink-nf7"], reg["devices"]["nf7"]
    assert probe["kind"] == "usb" and probe["match"] == {
        "usb_vid": "0483", "usb_pid": "374b", "usb_serial": "0671FF485570854967081043"}
    assert board["kind"] == "swd" and board["policy"] == {"board_tag": "nucleo-f767zi"}
    assert board["reach"]["swd"] == {"host": "bench-a", "probe": "stlink-nf7", "chip": "stm32f767",
                                     "uid": "003a001a3034511930363235", "state": "live",
                                     "can": ["flash", "diag"]}
    # its console is the probe's own serial port
    assert board["match"] == {"serial_by_id": PROBE_FACTS["serial_ports"][0]}
    assert board["reach"]["usb"] == {"host": "bench-a", "can": ["shell", "diag"]}
    assert set(reg["devices"]) == {"board7", "stlink-nf7", "nf7"}
    filed = yaml.safe_load((tmp / "facts" / "nf7.yaml").read_text())
    assert filed["registered"] == ["nf7", "stlink-nf7"]


def test_the_probes_name_can_be_given(probe_site):
    tmp, env, _facts = probe_site
    r = run(env, "nf7", "0671FF485570854967081043", "--probe-name", "nfprobe")
    assert r.returncode == 0, r.stderr
    reg = yaml.safe_load((tmp / "registry.yaml").read_text())
    assert reg["devices"]["nf7"]["reach"]["swd"]["probe"] == "nfprobe" and "nfprobe" in reg["devices"]


@pytest.mark.parametrize("change,says", [
    ({"probes": {"probe_drive": {"label": "NOD_F767ZI"},
                 "swd": {"board_type": "nucleo-f767zi", "chip": "stm32f767",
                         "error": "the debug port answers but the core does not"}}},
     "did not read the chip behind the probe (the debug port answers"),
    ({"probes": {"probe_drive": {"label": "NOD_F767ZI"}}}, "did not read the chip behind the probe"),
    ({"probes": {"probe_drive": {"label": "NOD_L476RG"},
                 "swd": {"chip": "stm32l476", "uid": "00112233445566778899aabb"}},
      "tags": ["probe-drive:NOD_L476RG"]}, "no board type claims the probe's drive (NOD_L476RG)"),
    ({"usb": {**PROBE_FACTS["usb"], "serial": "0001"}}, "has no USB serial of its own"),
])
def test_a_probe_board_with_something_unread_is_not_enrolled(probe_site, change, says):
    tmp, env, facts = probe_site
    facts(**change)
    r = run(env, "nf7", "0671FF485570854967081043" if "usb" not in change else "0001")
    assert r.returncode != 0 and says in r.stderr, r.stderr
    assert (tmp / "registry.yaml").read_text() == REGISTRY, "nothing written"


def test_a_chip_already_registered_is_not_enrolled_again(probe_site):
    tmp, env, _facts = probe_site
    reg = REGISTRY.replace("protected:", "  old-nf7:\n    kind: swd\n    reach:\n      swd: {probe: p, chip: "
                           "stm32f767, uid: 003A001A3034511930363235}\nprotected:")
    (tmp / "registry.yaml").write_text(reg)
    r = run(env, "nf7", "0671FF485570854967081043")
    assert r.returncode != 0 and "already registered as old-nf7" in r.stderr, r.stderr
    assert (tmp / "registry.yaml").read_text() == reg
