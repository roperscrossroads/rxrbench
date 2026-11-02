import atexit
import os
import shutil
import tempfile

import pytest
import yaml

from ansible_collections.rxrbench.core.plugins.module_utils import layout

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures")
MESH_CATALOG = os.path.join(FIX, "..", "..", "collections", "ansible_collections", "rxrbench", "mesh", "catalog")


def _catalog():
    """The fixture catalog with the mesh plugin's recipe file beside it, as a site's catalog directory
    holds both. Made once per run; the tree keeps no symlink."""
    d = tempfile.mkdtemp(prefix="rxrbench-catalog-")
    atexit.register(shutil.rmtree, d, True)
    for name in os.listdir(os.path.join(FIX, "catalog")):
        shutil.copy(os.path.join(FIX, "catalog", name), d)
    shutil.copy(os.path.join(MESH_CATALOG, "zephyrtastic.yml"), d)
    return d


CATALOG = _catalog()
REG = layout.load_registry(os.path.join(FIX, "registry.yaml"))

LORA = {"region": "US", "modem_preset": "SHORT_FAST", "tx_power": 2, "tx_enabled": True}



@pytest.fixture(autouse=True)
def built_in_rf_ceiling(tmp_path_factory, monkeypatch):
    """These tests are about a quiet bench in the US (2 dBm): the site's
    rules they run under say so, whatever the live site's say."""
    from ansible_collections.rxrbench.core.plugins.module_utils import site
    rules = tmp_path_factory.mktemp("rf") / "rf_rules.yml"
    rules.write_text("region: US\nmax_tx_power: 2\n")
    monkeypatch.setattr(site, "rf_rules_path", lambda: str(rules))


def make(tmp_path, boards, host_vars):
    hv = tmp_path / "host_vars"
    hv.mkdir(exist_ok=True)
    for name, doc in host_vars.items():
        (hv / f"{name}.yml").write_text(yaml.safe_dump(doc))
    return str(tmp_path), {"boards": boards, "catalog_dir": CATALOG}


def widget():
    """A board of the fixture plugin's widget family (the `widgets` fixture loads it)."""
    return {"firmware": {"family": "widget", "image": "widget-heltec-v4"}}


def beacon(tx_dbm=2, image="beacon-heltec-v4r8"):
    """A board of the fixture plugin's beacon family, whose LoRa radio this tool sets."""
    return {"firmware": {"family": "beacon", "image": image},
            "beacon": {"radio": {"tx_dbm": tx_dbm, "freq_mhz": 915.0, "bw_khz": 250}}}


@pytest.fixture
def lora_bands(monkeypatch):
    """What a radio plugin registers for LoRa in the US (rfrules.register_radio)."""
    from ansible_collections.rxrbench.core.plugins.module_utils import rfrules
    monkeypatch.setitem(rfrules.RADIOS, "lora", {"US": {"band_mhz": [902.0, 928.0], "max_tx_power": 30}})


def member(**zep):
    return {"firmware": {"family": "zephyrtastic", "class": 4}, "zephyrtastic": dict(zep),
            "meshtastic": {"config": {"lora": dict(LORA)}}}


def test_a_simulated_board_publishes_what_its_sim_bench_needs(tmp_path):
    """Its factory image, hardware id and seed, and the kind_sim group the host play uses to
    give its host a sim bench (and no broker unit of its own)."""
    d = tmp_path / "lay"
    (d / "host_vars").mkdir(parents=True)
    (d / "host_vars" / "zb1.yml").write_text("firmware: {family: bare, image: img1}\n")
    cat = tmp_path / "cat"
    cat.mkdir()
    (cat / "firmware.yml").write_text(
        "images:\n  img1: {family: bare, board_tags: [native-sim], flasher: sim, rf: none,\n"
        "         source: {type: path, path: /nowhere}}\n")
    (cat / "boards.yml").write_text("boards:\n  native-sim: {mcu: native-sim}\n")
    reg = {"hosts": {"vm1": {"ssh": "bench@127.0.0.1", "port": 2222}},
           "devices": {"zb1": {"kind": "sim", "policy": {"board_tag": "native-sim"},
                               "match": {"sim_device_id": 11}, "sim_exe": "zephyr/app/zephyr.exe",
                               "sim_seed": 7}}}
    r = layout.load_layout(str(d), {"boards": {"zb1": {"host": "vm1"}}, "catalog_dir": str(cat),
                                    "location_dir": str(tmp_path / "none")}, reg=reg)
    b = r["boards"]["zb1"]
    assert "kind_sim" in b["groups"] and b["vars"]["bench_kind"] == "sim"
    assert b["vars"]["bench_sim_exe"] == "zephyr/app/zephyr.exe"
    assert b["vars"]["bench_sim_device_id"] == 11 and b["vars"]["bench_sim_seed"] == 7
    assert b["connection"]["ansible_port"] == 2222


def test_a_bench_host_behind_a_forwarded_port_has_its_port_key_and_known_hosts():
    """A simulated bench host (a VM on this machine) is reached on a loopback port with a key
    of its own; the registry's hosts entry says so and the connection carries it."""
    reg = {"hosts": {"vm1": {"ssh": "bench@127.0.0.1", "port": 2222, "ssh_key": "~/k",
                             "known_hosts": "~/kh"}}}
    c = layout.host_connection(reg, "vm1")
    assert c["ansible_host"] == "127.0.0.1" and c["ansible_user"] == "bench" and c["ansible_port"] == 2222
    assert c["ansible_ssh_private_key_file"] == os.path.expanduser("~/k")
    assert "UserKnownHostsFile=" + os.path.expanduser("~/kh") in c["ansible_ssh_common_args"]
    assert layout.host_connection({"hosts": {"b": {"ssh": "u@b"}}}, "b") == {"ansible_host": "b", "ansible_user": "u"}


def test_a_board_becomes_a_host_on_its_bench_host(widgets, tmp_path):
    d, cfg = make(tmp_path, {"member1": {"host": "benchA"}}, {"member1": widget()})
    r = layout.load_layout(d, cfg, REG)
    assert r["bench_hosts"] == {"benchA": {"ansible_host": "bencha", "ansible_user": "tester"}}
    b = r["boards"]["member1"]
    assert b["connection"] == {"ansible_host": "bencha", "ansible_user": "tester"}
    assert set(b["groups"]) == {"boards", "family_widget", "host_benchA", "kind_serial"}
    assert b["vars"]["bench_family"] == "widget" and r["warnings"] == []
    assert b["vars"]["bench_devreg"] == "member1" and b["vars"]["bench_fw_kind"] == "port"


def test_an_alias_resolves_to_its_registry_name(widgets, tmp_path):
    d, cfg = make(tmp_path, {"h1": {"host": "benchA"}}, {"h1": widget()})
    assert layout.load_layout(d, cfg, REG)["boards"]["h1"]["vars"]["bench_devreg"] == "head1"


def test_a_local_bench_host_uses_a_local_connection(tmp_path):
    d, cfg = make(tmp_path, {"stock1": {"host": "workstation"}}, {"stock1": {"zephyrtastic": {"stock": True}}})
    r = layout.load_layout(d, cfg, REG)
    assert r["boards"]["stock1"]["connection"] == {"ansible_connection": "local"}
    assert "stock" in r["boards"]["stock1"]["groups"]


def refused(tmp_path, boards, host_vars):
    d, cfg = make(tmp_path, boards, host_vars)
    with pytest.raises(layout.LayoutError) as e:
        layout.load_layout(d, cfg, REG)
    return str(e.value)


def test_a_board_unknown_to_the_registry_is_refused(tmp_path):
    assert "ghost: not in the device registry" in refused(tmp_path, {"ghost": {"host": "benchA"}}, {})


def test_a_board_without_a_bench_host_is_refused(tmp_path):
    assert "not seen on any bench host, and no host declared" in refused(tmp_path, {"member1": {}}, {"member1": member()})


def test_an_unknown_bench_host_is_refused(tmp_path):
    assert "benchZ" in refused(tmp_path, {"member1": {"host": "benchZ"}}, {"member1": member()})


def test_a_registry_host_pin_wins_over_the_layout(tmp_path):
    msg = refused(tmp_path, {"pinned1": {"host": "benchA"}}, {"pinned1": {}})
    assert "pins it to benchB" in msg


def test_rf_rules_are_enforced_at_load(tmp_path, widgets, lora_bands):
    assert "14 dBm is above the ceiling" in refused(tmp_path, {"member1": {"host": "benchA"}},
                                                    {"member1": beacon(14)})


def test_a_managed_radio_left_undeclared_is_refused_and_one_switched_off_passes(tmp_path, widgets, lora_bands):
    quiet = beacon()
    del quiet["beacon"]["radio"]
    assert "no beacon.radio declared" in refused(tmp_path, {"member1": {"host": "benchA"}}, {"member1": quiet})
    quiet["beacon"]["radio"] = {"tx": False, "tx_dbm": 20}
    d, cfg = make(tmp_path, {"member1": {"host": "benchA"}}, {"member1": quiet})
    assert "member1" in layout.load_layout(d, cfg, REG)["boards"]


def test_a_board_the_registry_mutes_may_not_be_given_tx(tmp_path):
    msg = refused(tmp_path, {"mute1": {"host": "benchA"}}, {"mute1": member()})
    assert "must never transmit" in msg


@pytest.mark.plugins
def test_a_brain_and_its_heads_must_agree(tmp_path):
    brain = member(brain={"heads": {"head1": "MEDIUM_FAST"}})
    brain["firmware"]["features"] = ["brain"]
    head = {"firmware": {"family": "zephyrtastic", "class": 12}, "zephyrtastic": {"head": {"brain": "member1"}},
            "meshtastic": {"config": {"lora": dict(LORA)}}}
    boards = {"brain1": {"host": "benchA"}, "head1": {"host": "benchA"}}
    msg = refused(tmp_path, boards, {"brain1": brain, "head1": head})
    assert "head head1 does not declare brain brain1" in msg
    assert "its brain member1 is not a board in this layout" in msg


@pytest.mark.plugins
def test_heads_need_the_brain_feature(tmp_path):
    brain = member(brain={"heads": {}})
    brain["zephyrtastic"]["brain"]["heads"] = {"head1": "MEDIUM_FAST"}
    head = {"firmware": {"family": "zephyrtastic", "class": 12}, "zephyrtastic": {"head": {"brain": "brain1"}},
            "meshtastic": {"config": {"lora": dict(LORA)}}}
    boards = {"brain1": {"host": "benchA"}, "head1": {"host": "benchA"}}
    assert "lacks the 'brain' feature" in refused(tmp_path, boards, {"brain1": brain, "head1": head})


def test_every_problem_is_reported_at_once(tmp_path, widgets, lora_bands):
    msg = refused(tmp_path, {"ghost": {"host": "benchA"}, "member1": {"host": "benchA"}},
                  {"member1": beacon(20)})
    assert "ghost" in msg and "20 dBm" in msg


@pytest.mark.plugins
def test_a_class_that_is_not_in_the_catalog_is_refused(tmp_path):
    bad = member()
    bad["firmware"]["class"] = 99
    assert "class 99 is not in the catalog" in refused(tmp_path, {"member1": {"host": "benchA"}},
                                                       {"member1": bad})


def test_an_image_built_for_another_board_type_is_refused(tmp_path, widgets, lora_bands):
    msg = refused(tmp_path, {"brain1": {"host": "benchA"}}, {"brain1": beacon()})
    assert "is a heltec-v4; its declared firmware is for heltec-v4r8" in msg


@pytest.mark.plugins
def test_a_class_built_for_another_board_type_is_refused(tmp_path):
    """zephyrtastic's classes name the board type they are built for."""
    r8 = member()
    r8["firmware"]["class"] = 6      # a V4-R8 class on a board the registry calls a V4
    msg = refused(tmp_path, {"brain1": {"host": "benchA"}}, {"brain1": r8})
    assert "is a heltec-v4; its declared firmware is for heltec-v4r8" in msg


def test_foreign_firmware_is_allowed_and_flagged_as_unmanaged_rf(tmp_path):
    hv = {"firmware": {"family": "bitpirate", "image": "bitpirate-heltec-v3"}}
    d, cfg = make(tmp_path, {"pinned1": {"host": "benchB"}}, {"pinned1": hv})
    r = layout.load_layout(d, cfg, REG)
    assert "family_bitpirate" in r["boards"]["pinned1"]["groups"]
    assert not any(g.startswith("class_") for g in r["boards"]["pinned1"]["groups"])
    rf, = [w for w in r["warnings"] if "own defaults" in w]
    assert "pinned1" in rf and "bitpirate" in rf
    plan, = [w for w in r["warnings"] if "no loaded plugin" in w]
    assert plan == ("pinned1: no loaded plugin brings its family bitpirate: the plan compares its "
                    "image only; its config and anything else declared for it are not compared")


def test_an_image_that_boots_out_of_band_is_carried_to_its_flasher_and_warned_of(tmp_path):
    hv = {"firmware": {"family": "bitpirate", "image": "loudboot-heltec-v3"}}
    d, cfg = make(tmp_path, {"pinned1": {"host": "benchB"}}, {"pinned1": hv})
    r = layout.load_layout(d, cfg, REG)
    boot = r["boards"]["pinned1"]["vars"]["bench_boot_radio_problems"]
    # (the band is the radio plugin's to register: tests/unit/test_boot_radio.py)
    assert any("ceiling of 2 dBm" in p for p in boot), boot
    assert any("first flash or a family switch" in w for w in r["warnings"])
    (tmp_path / "host_vars" / "pinned1.yml").write_text(yaml.safe_dump({**hv, "bench_boot_radio_ok": True}))
    r = layout.load_layout(d, cfg, REG)
    assert not any("first flash or a family switch" in w for w in r["warnings"])


def test_foreign_firmware_must_be_a_catalogued_image_for_this_board(tmp_path):
    hv = {"firmware": {"family": "bitpirate", "image": "no-such-image"}}
    assert "not in the firmware catalog" in refused(tmp_path, {"pinned1": {"host": "benchB"}},
                                                    {"pinned1": hv})
    hv = {"firmware": {"family": "bitpirate", "image": "bitpirate-heltec-v3"}}
    msg = refused(tmp_path, {"brain1": {"host": "benchA"}}, {"brain1": hv})
    assert "is a heltec-v4; its declared firmware is for heltec-v3" in msg


def test_an_image_declared_under_the_wrong_family_is_refused(tmp_path):
    hv = {"firmware": {"family": "meshcore", "image": "bitpirate-heltec-v3"}}
    msg = refused(tmp_path, {"pinned1": {"host": "benchB"}}, {"pinned1": hv})
    assert "is bitpirate firmware, the board declares meshcore" in msg


def test_a_board_the_plug_watcher_does_not_see_is_marked_unplugged(widgets, tmp_path, monkeypatch):
    import json
    import time
    idx = tmp_path / "index"
    idx.mkdir()
    (idx / "plug-benchA.json").write_text(json.dumps({"v": 2, "reported": ["SOMEOTHERBOARD00"]}))
    d, cfg = make(tmp_path, {"member1": {"host": "benchA"}}, {"member1": widget()})
    cfg["location_dir"] = str(idx)
    r = layout.load_layout(d, cfg, REG)
    b = r["boards"]["member1"]
    assert b["vars"]["bench_present"] is False and "absent" in b["groups"]
    assert any("not plugged in" in w for w in r["warnings"])
    # A board the watcher could not tell from any other (no chip id, no MAC in its registry
    # match) is not skipped for being unseen: assumed present, and the plan says so.
    import copy
    reg = copy.deepcopy(REG)
    reg["devices"]["member1"]["match"] = {"vid_pid": "10c4:ea60"}
    r = layout.load_layout(d, cfg, reg)
    b = r["boards"]["member1"]
    assert b["vars"]["bench_present"] is True and "absent" not in b["groups"]
    assert any("member1" in w and "assumed present" in w and "first read decides" in w
               for w in r["warnings"])
    assert not any("not plugged in" in w for w in r["warnings"])
    # Behind a USB bridge on its factory serial the board is just as nameless, and the warning
    # names the remedy: a serial of the bridge's own.
    reg["devices"]["member1"]["match"] = {
        "serial_by_id": "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0"}
    r = layout.load_layout(d, cfg, reg)
    assert r["boards"]["member1"]["vars"]["bench_present"] is True
    assert any("member1" in w and "factory serial 0001" in w and "cp210x_serial" in w
               for w in r["warnings"])
    # No fresh index at all: presence unknown, assumed present (the old behaviour).
    old = time.time() - 3600
    os.utime(idx / "plug-benchA.json", (old, old))
    assert layout.load_layout(d, cfg, REG)["boards"]["member1"]["vars"]["bench_present"] is True


def test_firmware_without_a_family_is_refused(tmp_path):
    hv = member()
    del hv["firmware"]["family"]
    assert "declares no family" in refused(tmp_path, {"member1": {"host": "benchA"}}, {"member1": hv})


def test_a_board_may_be_declared_by_a_directory_of_files(tmp_path):
    from ansible_collections.rxrbench.core.plugins.module_utils import layout as lay
    d = tmp_path / "host_vars" / "board9"
    d.mkdir(parents=True)
    (d / "10-board.yml").write_text("firmware: {family: zephyrtastic, class: 4}\nbench_flasher: esptool\n")
    (d / "20-route.yml").write_text("bench_flasher: smp\nroutes: {flash: ble}\n")
    (d / "30-keys.sops.yaml").write_text("sops: {}\n")
    assert lay.read_host_vars(str(tmp_path), "board9") == {
        "firmware": {"family": "zephyrtastic", "class": 4}, "bench_flasher": "smp",
        "routes": {"flash": "ble"}}


@pytest.mark.plugins
def test_a_head_takes_no_config_and_a_listener_no_live_check(tmp_path):
    """The family narrows a board's row and publishes its facts; the role reads those, never a
    group name."""
    brain = member(brain={"heads": {"head1": "MEDIUM_FAST"}})
    brain["firmware"]["features"] = ["brain"]
    head = {"firmware": {"family": "zephyrtastic", "class": 12}, "zephyrtastic": {"head": {"brain": "brain1"}},
            "meshtastic": {"config": {"lora": dict(LORA)}}}
    listener = member(listener={"presets": ["SHORT_FAST"]})
    boards = {"brain1": {"host": "benchA"}, "head1": {"host": "benchA"}, "member1": {"host": "benchA"}}
    d, cfg = make(tmp_path, boards, {"brain1": brain, "head1": head, "member1": listener})
    r = layout.load_layout(d, cfg, REG)["boards"]
    assert r["brain1"]["vars"]["bench_config_via"] == "shell-tunnel"
    assert r["brain1"]["vars"]["bench_radio_skip"] == [] and r["brain1"]["vars"]["bench_radio_check_live"] is True
    assert r["head1"]["vars"]["bench_config_via"] is None and r["head1"]["vars"]["bench_key_escrow"] is None
    assert r["head1"]["vars"]["bench_radio_skip"] == ["modem_preset"]
    assert r["member1"]["vars"]["bench_radio_check_live"] is False
    facts = r["member1"]["vars"]["bench_family_facts"]
    assert facts["radio_skip"] == [] and facts["radio_check_live"] is False
    assert (facts["led_mode"], facts["led_color"]) == ("in-use", "yellow"), "a listener's LED"
    assert r["head1"]["vars"]["bench_led_color"] == "cyan" and r["brain1"]["vars"]["bench_led_color"] == "magenta"
    assert r["member1"]["vars"]["bench_family_tasks"] == "zephyrtastic"


def test_a_boards_type_says_its_chip_and_a_type_the_catalog_lacks_is_refused(tmp_path, widgets):
    d, cfg = make(tmp_path, {"brain1": {"host": "benchA"}}, {"brain1": widget()})
    v = layout.load_layout(d, cfg, REG)["boards"]["brain1"]["vars"]
    assert (v["bench_board_tag"], v["bench_mcu"], v["bench_psram"]) == ("heltec-v4", "esp32s3", "2MB")
    assert v["bench_board_type"]["mcu"] == "esp32s3", "the type's whole record, for what core does not name"
    reg = layout.load_registry(os.path.join(FIX, "registry.yaml"))
    reg["devices"]["brain1"]["policy"]["board_tag"] = "heltec-v9"
    with pytest.raises(layout.LayoutError, match="board type 'heltec-v9' is not in the board catalog"):
        layout.load_layout(d, cfg, reg)


def test_a_familys_diagnostics_are_published_for_its_boards(tmp_path, widgets):
    d, cfg = make(tmp_path, {"member1": {"host": "benchA"}}, {"member1": widget()})
    v = layout.load_layout(d, cfg, REG)["boards"]["member1"]["vars"]
    assert [c["name"] for c in v["bench_diag_defaults"]] == ["version", "state"]


def test_a_layout_that_loosens_the_rf_ceiling_is_refused_and_one_that_tightens_it_carries_the_rules(tmp_path, widgets, lora_bands):
    d, cfg = make(tmp_path, {"member1": {"host": "benchA"}}, {"member1": beacon(10)})
    cfg["rf_rules"] = {"max_tx_power": 10}
    with pytest.raises(layout.LayoutError) as e:
        layout.load_layout(d, cfg, REG)
    assert "only lower it" in str(e.value) and "10 dBm is above the ceiling" in str(e.value)
    d, cfg2 = make(tmp_path, {"member1": {"host": "benchA"}}, {"member1": beacon(1)})
    cfg2["rf_rules"] = {"max_tx_power": 1}
    v = layout.load_layout(d, cfg2, REG)["boards"]["member1"]["vars"]
    assert v["bench_rf_rules"]["max_tx_power"] == 1 and v["bench_rf_rules"]["region"] == "US"


def test_a_layout_may_name_its_own_rf_ceiling(tmp_path, widgets, lora_bands):
    (tmp_path / "loose.yml").write_text("region: US\nmax_tx_power: 10\n")
    d, cfg = make(tmp_path, {"member1": {"host": "benchA"}}, {"member1": beacon(10)})
    cfg["rf_ceiling"] = "loose.yml"
    assert layout.load_layout(d, cfg, REG)["boards"]["member1"]["vars"]["bench_rf_rules"]["max_tx_power"] == 10


def test_a_muted_board_may_not_be_given_firmware_whose_radio_is_unmanaged(tmp_path):
    hv = {"firmware": {"family": "bitpirate", "image": "bitpirate-heltec-v4"}}
    msg = refused(tmp_path, {"mute1": {"host": "benchA"}}, {"mute1": hv})
    assert "must never transmit" in msg and "radio this tool does not set" in msg


def test_a_chip_behind_a_probe_is_there_when_its_probe_is(widgets, tmp_path):
    """A board of kind swd has no USB face of its own: with a fresh plug index, it is present
    when its probe is seen (by identity, or by model for a probe with no serial) and skipped
    with a warning when it is not; without an index it is assumed present, as before."""
    import json
    import time
    reg = {**REG, "devices": {**REG["devices"],
                              "pill1": {"kind": "swd", "firmware": "port", "policy": {"board_tag": "heltec-v4"},
                                        "reach": {"swd": {"host": "benchA", "probe": "probe1"}}},
                              "probe1": {"kind": "usb", "match": {"usb_vid": "0483", "usb_pid": "3748"}}}}
    d, cfg = make(tmp_path, {"pill1": {"host": "benchA"}}, {"pill1": widget()})
    loc = tmp_path / "loc"
    loc.mkdir()
    cfg["location_dir"] = str(loc)

    def index(devices):
        (loc / "plug-benchA.json").write_text(json.dumps({"v": 2, "reported": ["AAAAAAAAAAAAAAA2"],
                                                           "devices": devices}))
        os.utime(loc / "plug-benchA.json", (time.time(), time.time()))

    index({"1-1": {"busid": "1-1", "usb_vid": "303a", "usb_pid": "1001", "serial": "AAAAAAAAAAAAAAA2"}})
    r = layout.load_layout(d, cfg, reg)
    assert r["boards"]["pill1"]["vars"]["bench_present"] is False
    assert any("pill1: its probe probe1 is not plugged in" in w for w in r["warnings"])
    index({"1-1": {"busid": "1-1", "usb_vid": "303a", "usb_pid": "1001", "serial": "AAAAAAAAAAAAAAA2"},
           "1-2": {"busid": "1-2", "usb_vid": "0483", "usb_pid": "3748", "serial": ""}})
    r = layout.load_layout(d, cfg, reg)
    assert r["boards"]["pill1"]["vars"]["bench_present"] is True and r["warnings"] == []
    os.remove(loc / "plug-benchA.json")
    r = layout.load_layout(d, cfg, reg)
    assert r["boards"]["pill1"]["vars"]["bench_present"] is True


def test_a_check_depth_other_than_full_or_spot_is_refused(tmp_path):
    hv = {"firmware": {"family": "bitpirate", "image": "bitpirate-heltec-v3"}, "bench_check_depth": "quick"}
    d, cfg = make(tmp_path, {"pinned1": {"host": "benchB"}}, {"pinned1": hv})
    with pytest.raises(layout.LayoutError, match="bench_check_depth is 'quick': full .the default. or spot"):
        layout.load_layout(d, cfg, REG)


def test_bench_overwrite_is_a_list_of_region_labels(tmp_path):
    hv = {"firmware": {"family": "bitpirate", "image": "bitpirate-heltec-v3"}, "bench_overwrite": "all"}
    d, cfg = make(tmp_path, {"pinned1": {"host": "benchB"}}, {"pinned1": hv})
    with pytest.raises(layout.LayoutError, match=r"bench_overwrite is 'all': a list of partition labels, or \[all\]"):
        layout.load_layout(d, cfg, REG)
    hv["bench_overwrite"] = ["all"]
    d, cfg = make(tmp_path, {"pinned1": {"host": "benchB"}}, {"pinned1": hv})
    assert layout.load_layout(d, cfg, REG)["boards"]["pinned1"]


def test_bench_foreign_is_info_warn_or_refuse(tmp_path):
    hv = {"firmware": {"family": "bitpirate", "image": "bitpirate-heltec-v3"}, "bench_foreign": "loud"}
    d, cfg = make(tmp_path, {"pinned1": {"host": "benchB"}}, {"pinned1": hv})
    with pytest.raises(layout.LayoutError, match="bench_foreign is 'loud': info .the default., warn or refuse"):
        layout.load_layout(d, cfg, REG)


def test_a_probe_reached_board_keeps_a_flasher_that_writes_over_the_probe():
    """A flash route on the swd bearer takes the image's flasher when it writes over swd (a
    plugin's probe flasher, FLASHER_BEARERS), core's swd flasher when the image names none."""
    assert layout.probe_flasher(None, None) == ("swd", None)
    assert layout.probe_flasher("swd", ("swd",)) == ("swd", None)
    assert layout.probe_flasher("vendor-probe", ("swd",)) == ("vendor-probe", None)
    flasher, why = layout.probe_flasher("esptool", ("usb",))
    assert flasher is None and why.endswith("but its image's flasher 'esptool' writes over usb")
    flasher, why = layout.probe_flasher("mystery", None)
    assert flasher is None and "FLASHER_BEARERS" in why


def test_bearers_come_from_core_and_from_a_plugins_declaration(monkeypatch):
    import types
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families, registry
    mod = types.SimpleNamespace(FAMILIES={}, PLUGINS={}, FLASHERS={"vendor-probe": ()},
                                FLASHER_BEARERS={"vendor-probe": ["swd"]})
    monkeypatch.setattr(registry, "load", lambda names, roots=(): {"fixture.probe": mod})
    try:
        families.load(["fixture.probe"])
        assert families.flasher_bearers("vendor-probe") == ("swd",)
        assert families.flasher_bearers("esptool") == ("usb",)
        assert families.flasher_bearers("nothing") is None
    finally:
        monkeypatch.undo()
        families.load()


def test_bench_uf2_enter_as_names_a_loaded_family_that_says_how_to_ask_for_uf2_mode(tmp_path, widgets):
    """bench_uf2_enter_as (docs/ARCHITECTURE.md#entering-uf2-mode): the name is checked when the layout loads."""
    named = {**widget(), "bench_uf2_enter_as": "widget"}
    d, cfg = make(tmp_path, {"member1": {"host": "benchA"}}, {"member1": named})
    assert "member1" in layout.load_layout(d, cfg, REG)["boards"]
    for bad, why in (("nosuch", "no loaded family"), ("gadget", "says nothing of how to ask")):
        msg = refused(tmp_path, {"member1": {"host": "benchA"}},
                      {"member1": {**widget(), "bench_uf2_enter_as": bad}})
        assert f"bench_uf2_enter_as is {bad!r}" in msg and why in msg and "widget" in msg


def test_what_a_board_makes_another_device_transmit_is_judged_too(tmp_path, widgets, lora_bands):
    """A family's rf_check: the fixture beacon commands a relay, whose power the ceiling holds."""
    loud = beacon()
    loud["beacon"]["relay"] = {"tx_dbm": 20}
    msg = refused(tmp_path, {"member1": {"host": "benchA"}}, {"member1": loud})
    assert "beacon.relay.tx_dbm" in msg and "20 dBm is above the ceiling" in msg
    loud["beacon"]["relay"] = {"tx_dbm": 1}
    d, cfg = make(tmp_path, {"member1": {"host": "benchA"}}, {"member1": loud})
    assert "member1" in layout.load_layout(d, cfg, REG)["boards"]


def test_flash_ranges_that_overlap_are_refused_when_the_layout_loads(tmp_path, widgets, monkeypatch):
    monkeypatch.setattr(type(widgets.plugin("widget")), "flash_regions", (
        {"label": "boot", "kind": "boot", "start": "0x0", "size": "0x8000"},
        {"label": "app", "kind": "app", "start": "0x4000", "size": "0x8000"}))
    msg = refused(tmp_path, {"member1": {"host": "benchA"}}, {"member1": widget()})
    assert "member1: flash regions: boot and app overlap at 0x4000" in msg
