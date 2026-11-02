"""The contracts a plugin fills in: how a device is found, what secrets it holds."""
import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import layout

from tests.unit.test_layout import REG, make, member, built_in_rf_ceiling  # noqa: F401  (the site's rules)


def boards_of(tmp_path, host_vars):
    d, cfg = make(tmp_path, {name: {"host": "benchA"} for name in host_vars}, host_vars)
    return layout.load_layout(d, cfg, REG)["boards"]


@pytest.mark.plugins
def test_a_device_is_found_by_its_familys_indicator_or_the_one_its_layout_names(tmp_path):
    wired = member()
    wired["bench_locate"] = "buzzer"
    r = boards_of(tmp_path, {"member1": member(), "brain1": wired})
    assert r["member1"]["vars"]["bench_locate"] == "shell"
    assert r["brain1"]["vars"]["bench_locate"] == "buzzer"
    assert r["brain1"]["vars"]["bench_led"] == "shell", "the family's own word is still published"


def kinds(r, board):
    return {s["kind"]: s for s in r[board]["vars"]["bench_secret_kinds"]}


@pytest.mark.plugins
def test_a_family_says_what_secrets_a_board_holds_and_a_layout_may_leave_one_alone(tmp_path):
    quiet = member()
    quiet["bench_secret_policy"] = {"identity": {"escrow": False}}
    r = boards_of(tmp_path, {"member1": member(), "brain1": quiet})
    k = kinds(r, "member1")
    assert k["identity"] == {"kind": "identity", "origin": "device", "via": "tunnel",
                             "tasks": "identity_tunnel", "escrow": True}
    assert k["channel_keys"]["origin"] == "declared" and k["channel_keys"]["escrow"] is False
    assert r["member1"]["vars"]["bench_key_escrow"] == "tunnel"
    assert kinds(r, "brain1")["identity"]["escrow"] is False
    assert r["brain1"]["vars"]["bench_key_escrow"] is None, "its own tasks leave the key alone too"


def test_the_escrow_file_keeps_every_kind_under_its_family():
    from ansible_collections.rxrbench.core.plugins.filter import escrow
    record = {"id": "0x01", "value": "KEY", "fingerprint": "ab12"}
    doc = escrow.escrow_put({}, "b1", "fam", "identity", record, "T")
    assert doc == {"board": "b1", "secrets": {"fam": {"identity": {**record, "exported": "T"}}}}
    assert escrow.escrow_get(doc, "fam", "identity") == {**record, "exported": "T"}
    doc = escrow.escrow_put(doc, "b1", "fam", "host_key", record, "T")
    assert doc["secrets"]["fam"] == {"identity": {**record, "exported": "T"},
                                     "host_key": {**record, "exported": "T"}}
    assert escrow.escrow_get(doc, "fam", "host_key")["value"] == "KEY"
    assert escrow.escrow_get(doc, "other", "identity") == {} and escrow.escrow_get(None, "f", "k") == {}


def test_a_verdict_names_a_secret_by_its_id_and_never_shows_it():
    from ansible_collections.rxrbench.core.plugins.filter.escrow import secret_verdict as verdict
    dev = {"kind": "identity", "origin": "device", "escrow": True}
    read = {"id": "0x01", "value": "KEY"}
    assert "NOT ESCROWED" in verdict(dev, read, {})
    assert verdict(dev, read, {"id": "0x01", "value": "KEY"}) == "0x01, matches the escrow"
    assert "DIFFERS from the escrow (0x02)" in verdict(dev, read, {"id": "0x02", "value": "OTHER"})
    assert "cannot be read (no answer)" in verdict(dev, {}, {})
    assert "this layout's choice" in verdict({**dev, "escrow": False}, read, {})
    dec = {"kind": "admin_password", "origin": "declared"}
    assert verdict(dec, {"declared": False}) == "not declared"
    assert verdict(dec, {"id": "admin", "declared": True, "matches": True}) == "admin, as declared"
    assert "DIFFERS from what is declared" in verdict(dec, {"id": "admin", "declared": True, "matches": False})
    for text in (verdict(dev, read, {"id": "0x02", "value": "OTHER"}), verdict(dev, read, {})):
        assert "KEY" not in text and "OTHER" not in text
    # Found on hardware: a node's id need not follow its key (a stock Meshtastic node's comes
    # from its MAC), so a key that differs under the same id is named by both fingerprints.
    same_id = verdict(dev, {**read, "fingerprint": "aa11"},
                      {"id": "0x01", "value": "OTHER", "fingerprint": "bb22"})
    assert same_id == "0x01 (key aa11) DIFFERS from the escrow (0x01, key bb22)", same_id


def test_a_plugin_brings_what_a_region_allows_its_kind_of_radio(monkeypatch):
    """Core holds no numbers: each emission is judged by the limits registered for its radio."""
    from ansible_collections.rxrbench.core.plugins.module_utils import rfrules
    monkeypatch.setattr(rfrules, "RADIOS", {})
    rfrules.register_radio("lora", {"US": {"band_mhz": [902.0, 928.0], "max_tx_power": 30}})
    rfrules.register_radio("ble", {"US": {"band_mhz": [2400.0, 2483.5], "max_tx_power": 20}})
    rules = rfrules.with_region({**rfrules.defaults(), "region": "US"})
    assert rules["max_tx_power"] == 30 and rules["band_mhz"] is None, "two radios: no one band is the site's"
    ok = [{"radio": "ble", "key": "tx", "power_dbm": 8, "freq_mhz": 2440.0, "bw_khz": 2000},
          {"radio": "lora", "key": "tx_power", "power_dbm": 30, "freq_mhz": 915.0, "bw_khz": 250}]
    assert rfrules.check_emissions("b", ok, rules) == []
    (loud,) = rfrules.check_emissions("b", [{"radio": "ble", "key": "tx", "power_dbm": 25}], rules)
    assert "above the ceiling of 20 dBm" in loud
    (off,) = rfrules.check_emissions("b", [{"radio": "ble", "key": "tx", "power_dbm": 8,
                                            "freq_mhz": 915.0, "bw_khz": 250}], rules)
    assert "outside the band 2400.0-2483.5" in off
    # with no radio registered there is nothing to judge a region by
    monkeypatch.setattr(rfrules, "RADIOS", {})
    assert rfrules.with_region({**rfrules.defaults(), "region": "ANYWHERE"})["max_tx_power"] is None


# ---- the Meshtastic keys ------------------------------------------------------------------

from tests.unit.test_layout import refused  # noqa: E402

KEY = "hex:" + "ab" * 32


def with_security(sec):
    m = member()
    m["meshtastic"]["config"]["security"] = sec
    return m


@pytest.mark.plugins
def test_admin_keys_are_a_declared_secret_of_a_board_that_takes_configuration(tmp_path):
    r = boards_of(tmp_path, {"member1": with_security({"admin_key": [KEY], "is_managed": True})})
    k = kinds(r, "member1")
    assert k["admin_keys"] == {"kind": "admin_keys", "origin": "declared", "tasks": "admin_keys",
                               "escrow": False}


@pytest.mark.plugins
def test_a_managed_node_with_no_admin_key_is_refused_by_the_layout_check(tmp_path):
    boards = {"member1": {"host": "benchA"}}
    msg = refused(tmp_path, boards, {"member1": with_security({"is_managed": True})})
    assert "member1: security.is_managed needs at least one security.admin_key" in msg
    msg = refused(tmp_path, boards, {"member1": with_security({"is_managed": True, "admin_key": []})})
    assert "needs at least one" in msg
    assert "32-byte" in refused(tmp_path, boards, {"member1": with_security({"admin_key": ["hex:00"]})})


@pytest.mark.plugins
def test_admin_keys_the_sites_secrets_give_at_run_time_pass_the_layout_check(tmp_path):
    for keys in (["{{ site_admins.a }}", KEY], "{{ site_admins | list }}"):
        r = boards_of(tmp_path, {"member1": with_security({"admin_key": keys, "is_managed": True})})
        assert "member1" in r
    boards = {"member1": {"host": "benchA"}}
    many = with_security({"admin_key": ["{{ a }}", "{{ b }}", "{{ c }}", "{{ d }}"]})
    assert "at most 3" in refused(tmp_path, boards, {"member1": many})


@pytest.mark.plugins
def test_a_keyless_head_takes_no_security_settings(tmp_path):
    brain = member(brain={"heads": {"head1": "MEDIUM_FAST"}})
    brain["firmware"]["features"] = ["brain"]
    head = with_security({"admin_key": [KEY]})
    head["firmware"]["class"] = 12
    head["zephyrtastic"] = {"head": {"brain": "brain1"}}
    boards = {"brain1": {"host": "benchA"}, "head1": {"host": "benchA"}}
    assert "head1: a keyless head has no phone API" in refused(tmp_path, boards,
                                                              {"brain1": brain, "head1": head})


@pytest.mark.plugins
def test_a_stock_meshtastic_boards_identity_is_read_over_its_serial_port():
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families
    assert [dict(s) for s in families.plugin("meshtastic").board_secrets({})] == [
        {"kind": "identity", "origin": "device", "via": "serial"}]


# ---- how a play reaches a device, and what can be done to it ---------------------------

def test_a_play_asks_how_a_command_reaches_a_device_and_what_its_flasher_can_do(widgets, tmp_path):
    from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
    r = boards_of(tmp_path, {"member1": {"firmware": {"family": "widget", "image": "widget-heltec-v4"}}})
    v = r["member1"]["vars"]
    assert v["bench_via"] == "console" and "no_console" not in r["member1"]["groups"]
    assert v["bench_can"] == common.can(v.get("bench_flasher"))
    assert common.can("esptool") == ["regions", "chip"]
    assert common.can("uf2") == [] and common.can(None) == []


def test_a_device_whose_shell_is_ssh_has_no_console_for_a_broker_to_own():
    from ansible_collections.rxrbench.core.plugins.module_utils import layout
    assert layout.via_of({"shell": {"bearer": "ssh"}, "flash": {"bearer": "ssh"}}) == "ssh"
    assert layout.via_of({"shell": {"bearer": "usb"}}) == "console"
    assert layout.via_of({"flash": {"bearer": "swd"}}) is None


def test_a_device_of_a_family_with_no_console_gets_no_broker(tmp_path):
    """Its shell route is its serial port, but the tool has no console for its family: a broker
    there would hold the port for nothing, and against whatever does drive the device."""
    d = tmp_path / "lay"
    (d / "host_vars").mkdir(parents=True)
    (d / "host_vars" / "wit.yml").write_text("firmware: {family: witness, image: img1}\n")
    cat = tmp_path / "cat"
    cat.mkdir()
    (cat / "boards.yml").write_text("boards:\n  bluepill-f103: {mcu: stm32f103}\n")
    (cat / "firmware.yml").write_text(
        "images:\n  img1: {family: witness, board_tags: [bluepill-f103], flasher: esptool,\n"
        "         rf: unmanaged, source: {type: path, path: /nowhere}}\n")
    entry = {"kind": "serial", "policy": {"board_tag": "bluepill-f103"},
             "reach": {"usb": {"host": "benchpi1", "state": "live",
                               "can": ["shell", "flash", "config", "diag"]}}}
    reg = {"hosts": {"benchpi1": {"ssh": "bench@benchpi1"}}, "devices": {"wit": entry}}
    r = layout.load_layout(str(d), {"boards": {"wit": {}}, "catalog_dir": str(cat),
                                    "location_dir": str(tmp_path / "none")}, reg=reg)
    b = r["boards"]["wit"]
    assert b["vars"]["bench_via"] == "console" and "no_console" in b["groups"]


@pytest.mark.plugins
def test_a_plugin_brings_the_board_types_its_devices_are():
    """Core's catalog holds the types that belong to no plugin; each plugin's is layered over
    it, then the site's."""
    from ansible_collections.rxrbench.core.plugins.module_utils import layout, site
    from ansible_collections.rxrbench.core.plugins.plugin_utils import registry
    dirs = registry.catalog_dirs()
    assert dirs[0] == site.CATALOG and dirs[-1].startswith(site.site_dir())
    own = set(layout.load_catalog(site.CATALOG)["boards"])
    merged = layout.load_catalog(*dirs)["boards"]
    # core's own is the simulated board alone; a chip behind a probe is a site's or a plugin's
    assert own == {"native-sim"}
    assert {"xiao", "heltec-v4", "raspberrypi4-64"} <= set(merged)
    assert merged["xiao"]["uf2"]["bootloader_usb"] == ["2886:0045"]
