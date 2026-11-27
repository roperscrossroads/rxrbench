from ansible_collections.rxrbench.core.plugins.module_utils import rflayout
import re

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import rfrules
from ansible_collections.rxrbench.mesh.plugins.module_utils import meshrf

# The rules these tests are about: a quiet bench in the US. Nothing of it is built in, so it is
# stated here and stands where a test gives no rules of its own.
QUIET = {"region": "US", "max_tx_power": 2}


@pytest.fixture(autouse=True)
def a_quiet_bench_in_the_us(monkeypatch):
    rules = rfrules.with_region({**meshrf.DEFAULT_RULES, **QUIET})
    monkeypatch.setattr(rflayout, "defaults", lambda: dict(rules))
    monkeypatch.setattr(meshrf, "DEFAULT_RULES", dict(rules))


def board(preset="SHORT_FAST", power=2, tx=True, **zep):
    return {"firmware": {"family": "zephyrtastic", "class": 4}, "zephyrtastic": dict(zep),
            "meshtastic": {"config": {"lora": {"modem_preset": preset, "tx_power": power,
                                               "tx_enabled": tx}}}}


def test_a_quiet_legal_transmitter_is_clean():
    assert rflayout.check_board("a", board()) == []


def test_power_above_the_ceiling_is_refused():
    (v,) = rflayout.check_board("a", board(power=14))
    assert "14 dBm" in v and "2 dBm" in v


def test_power_zero_is_the_region_maximum_and_is_refused():
    (v,) = rflayout.check_board("a", board(power=0))
    assert "region maximum" in v


def test_a_missing_power_or_preset_is_refused_not_assumed():
    hv = board()
    del hv["meshtastic"]["config"]["lora"]["tx_power"]
    del hv["meshtastic"]["config"]["lora"]["modem_preset"]
    assert len(rflayout.check_board("a", hv)) == 2


def test_a_board_with_firmware_but_no_lora_block_is_flagged():
    (v,) = rflayout.check_board("a", {"firmware": {"family": "zephyrtastic", "class": 4}})
    assert "unmanaged" in v


def test_rules_are_data():
    assert rflayout.check_board("a", board(power=10), {"max_tx_power": 10}) == []
    assert rflayout.check_board("a", board(preset="LONG_FAST"), {"max_tx_power": 10}) == []


# ---- MeshCore (zephcore) ----------------------------------------------------------------

def mc(radio=None, **block):
    return {"firmware": {"family": "zephcore", "role": "repeater"},
            "meshcore": {**({"radio": radio} if radio is not None else {}), **block}}


def test_a_meshcore_board_on_the_usa_preset_at_2_dbm_passes():
    assert rflayout.check_board("z", mc({"preset": "usa", "tx_dbm": 2, "tx_enabled": True})) == []


def test_a_meshcore_board_above_the_ceiling_or_at_zero_is_refused():
    assert "above the ceiling" in rflayout.check_board("z", mc({"preset": "usa", "tx_dbm": 10,
                                                               "tx_enabled": True}))[0]
    assert "region maximum" in rflayout.check_board("z", mc({"preset": "usa", "tx_dbm": 0,
                                                            "tx_enabled": True}))[0]


def test_a_channel_reaching_out_of_the_band_is_refused():
    v = rflayout.check_board("z", mc({"freq_mhz": 927.95, "bw_khz": 250, "sf": 9, "cr": 5,
                                      "tx_dbm": 2, "tx_enabled": True}))
    assert v and "outside the band" in v[0]


def test_a_receive_only_meshcore_board_is_not_checked_for_power():
    assert rflayout.check_board("z", mc({"preset": "usa", "tx_dbm": 10})) == []
    assert rflayout.transmits(mc({"preset": "usa", "tx_dbm": 10})) is False


def test_a_zephcore_board_with_no_radio_declared_is_refused():
    assert "no meshcore.radio declared" in rflayout.check_board("z", mc())[0]


def test_an_unknown_preset_is_refused():
    assert "unknown MeshCore preset" in rflayout.check_board("z", mc({"preset": "eu",
                                                                     "tx_enabled": True}))[0]


# ---- the ceiling ------------------------------------------------------------------------------

def test_no_region_no_transmitter(tmp_path, monkeypatch):
    """Nothing is built in: a site that has not said where it is has no rules, and a board
    that would transmit is refused until it does."""
    monkeypatch.undo()
    none = rflayout.ceiling(str(tmp_path / "none.yml"))
    assert none == {"region": None, "max_tx_power": None, "band_mhz": None}
    (v,) = rflayout.check_board("a", board(), none)
    assert "no region is set" in v and "region: US" in v
    assert rflayout.check_board("a", board(tx=False), none) == []


def test_a_region_gives_the_band_and_the_power_the_firmware_ships_for_it(tmp_path):
    (tmp_path / "rf.yml").write_text("region: US\n")
    us = rflayout.ceiling(str(tmp_path / "rf.yml"))
    assert us["band_mhz"] == [902.0, 928.0] and us["max_tx_power"] == 30
    # the firmware's own defaults pass: full power, power left to the firmware, the public preset
    assert rflayout.check_board("a", board(preset="LONG_FAST", power=30), us) == []
    assert rflayout.check_board("a", board(preset="LONG_FAST", power=0), us) == []
    assert "above the ceiling of 30 dBm" in rflayout.check_board("a", board(power=31), us)[0]
    (tmp_path / "jp.yml").write_text("region: JP\n")
    assert rflayout.ceiling(str(tmp_path / "jp.yml"))["max_tx_power"] == 13


def test_a_board_declared_for_another_region_is_refused(tmp_path):
    hv = board()
    hv["meshtastic"]["config"]["lora"]["region"] = "EU_868"
    (v,) = rflayout.check_board("a", hv)
    assert "declared for region EU" in v and "this site is in US" in v
    hv["meshtastic"]["config"]["lora"]["region"] = "US"
    assert rflayout.check_board("a", hv) == []
    (v,) = meshrf.check_write("meshtastic", "a", {"region": "EU_868", "modem_preset": "SHORT_FAST"})
    assert "region EU" in v
    # a European channel on a MeshCore board is outside the US band
    assert "outside the band" in rflayout.check_board("z", mc({
        "freq_mhz": 869.525, "bw_khz": 250, "sf": 11, "cr": 5, "tx_dbm": 2, "tx_enabled": True}))[0]


def test_a_site_may_ask_for_less_than_its_region_allows_never_more(tmp_path):
    (tmp_path / "rf.yml").write_text("region: US\nmax_tx_power: 5\n")
    assert rflayout.ceiling(str(tmp_path / "rf.yml"))["max_tx_power"] == 5
    for text, why in (("region: US\nmax_tx_power: 33\n", "above what region US allows"),
                      ("region: US\nband_mhz: [863.0, 870.0]\n", "outside region US"),
                      ("region: MARS\n", "unknown region"),
                      ("max_power: 5\n", "unknown RF rule(s) max_power")):
        (tmp_path / "bad.yml").write_text(text)
        with pytest.raises(ValueError, match=re.escape(why)):
            rflayout.ceiling(str(tmp_path / "bad.yml"))


def test_a_layout_may_only_tighten_the_ceiling():
    c = rfrules.with_region({**meshrf.DEFAULT_RULES, **QUIET})
    rules, problems = rflayout.tighten(c, {"max_tx_power": 1, "band_mhz": [903.0, 927.0]})
    assert problems == []
    assert rules == {"region": "US", "max_tx_power": 1, "band_mhz": [903.0, 927.0]}
    rules, problems = rflayout.tighten(c, {"max_tx_power": 10, "band_mhz": [900.0, 930.0]})
    assert len(problems) == 2 and "only lower" in problems[0] and "only narrow" in problems[1]
    assert rules == c, "the ceiling stands where a layout loosened it"
    assert "unknown rule" in rflayout.tighten(c, {"max_power": 1})[1][0]
    assert "another region" in rflayout.tighten(c, {"region": "EU"})[1][0]


# ---- at the write: the block a module holds, whatever file or flag it came from --------------

def test_a_write_above_the_ceiling_is_refused():
    assert meshrf.check_write("meshtastic", "a", {"modem_preset": "LONG_FAST", "tx_power": 2}) == []
    (v,) = meshrf.check_write("meshtastic", "a", {"modem_preset": "SHORT_FAST", "tx_power": 20})
    assert "20 dBm" in v
    assert meshrf.check_write("meshtastic", "a", {"modem_preset": "SHORT_FAST", "tx_power": 2}) == []


def test_a_write_is_judged_by_the_layouts_rules_when_it_is_given_them():
    rules = {"max_tx_power": 10}
    assert meshrf.check_write("meshtastic", "a", {"modem_preset": "SHORT_FAST", "tx_power": 10}, rules) == []
    (v,) = meshrf.check_write("meshtastic", "a", {"modem_preset": "SHORT_FAST", "tx_power": 11}, rules)
    assert "11 dBm" in v


def test_a_write_judges_only_the_keys_it_carries():
    # A head's preset is its brain's to set: its block arrives without one.
    assert meshrf.check_write("meshtastic", "h", {"tx_power": 2, "tx_enabled": True}) == []


def test_a_write_that_leaves_the_transmitter_off_keys_nothing_up():
    loud = {"modem_preset": "LONG_FAST", "tx_power": 30}
    assert meshrf.check_write("meshtastic", "a", {**loud, "tx_enabled": False}) == []
    # A block that does not say takes the board's own switch, and an unknown switch is on.
    assert meshrf.check_write("meshtastic", "a", loud, tx_now=False) == []
    assert len(meshrf.check_write("meshtastic", "a", loud, tx_now=True)) == 1
    assert len(meshrf.check_write("meshtastic", "a", loud)) == 1


def test_a_meshcore_write_is_held_to_the_power_and_the_band():
    ok = {"freq_mhz": 910.525, "bw_khz": 62.5, "tx_dbm": 2, "tx_enabled": True}
    assert meshrf.check_write("meshcore", "r", ok) == []
    (v,) = meshrf.check_write("meshcore", "r", {**ok, "tx_dbm": 20})
    assert "20 dBm" in v
    (v,) = meshrf.check_write("meshcore", "r", {**ok, "freq_mhz": 869.525})
    assert "outside the band" in v
    assert meshrf.check_write("meshcore", "r", {**ok, "tx_dbm": 20, "tx_enabled": False}) == []
    # zephcore boots receive-only: a block that leaves the switch alone on such a board is quiet.
    assert meshrf.check_write("meshcore", "r", {"tx_dbm": 20}, tx_now=False) == []
    assert len(meshrf.check_write("meshcore", "r", {"tx_dbm": 20}, tx_now=True)) == 1


def test_the_radio_writers_take_the_rules_and_the_role_hands_them_over():
    import os
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    mesh = os.path.join(repo, "collections", "ansible_collections", "rxrbench", "mesh")
    mods = os.path.join(mesh, "plugins", "modules")
    tasks = os.path.join(mesh, "roles", "board", "tasks")
    for mod, task in (("meshtastic_radio", "meshtastic.yml"), ("meshcore_radio", "meshcore.yml")):
        src = open(os.path.join(mods, mod + ".py")).read()
        assert '"rf_rules": {"type": "dict"}' in src and "the RF rules refuse this write" in src, mod
        call = open(os.path.join(tasks, task)).read().split(f"rxrbench.mesh.{mod}:")[1].split("\n\n")[0]
        assert 'rf_rules: "{{ bench_rf_rules | default(omit) }}"' in call, task


# ---- a firmware whose radio is set only when declared (stock Meshtastic) -----------------------

def stock(lora=None):
    return {"firmware": {"family": "meshtastic", "image": "x"},
            "meshtastic": {"config": {"lora": lora}} if lora is not None else {}}


def test_stock_with_its_radio_declared_is_not_warned_about_and_is_checked():
    # a stock board whose layout declares its power: no warning that the power is the
    # firmware's own, since apply writes it and the RF rules check it
    hv = stock({"region": "US", "modem_preset": "MEDIUM_FAST", "tx_power": 2})
    assert rflayout.warnings({"h": hv}) == []
    assert rflayout.transmits(hv) is True
    (v,) = rflayout.check_board("h", stock({"region": "US", "modem_preset": "MEDIUM_FAST",
                                            "tx_power": 14}))
    assert "14 dBm" in v


def test_stock_declared_silent_does_not_transmit():
    assert rflayout.transmits(stock({"region": "US", "tx_enabled": False})) is False


def test_stock_with_no_radio_declared_is_warned_about_and_counts_as_transmitting():
    (w,) = rflayout.warnings({"h": stock()})
    assert "own defaults" in w
    assert rflayout.transmits(stock()) is True


# ---- a preset's width, and the presets a brain sets on its heads (rf_check) ------------------

EU = {"region": "EU"}       # 869.4-869.65 MHz: 250 kHz wide


def test_a_presets_width_must_fit_the_band():
    from ansible_collections.rxrbench.core.plugins.module_utils import rfrules
    eu, us = rfrules.with_region({**rfrules.defaults(), **EU}), rfrules.with_region({**rfrules.defaults(), "region": "US"})
    assert meshrf.preset_problems("n1", "SHORT_TURBO", us) == []
    (wide,) = meshrf.preset_problems("n1", "SHORT_TURBO", eu)
    assert "500 kHz wide, wider than the band 869.4-869.65" in wide
    assert meshrf.preset_problems("n1", "LONG_FAST", eu) == [], "250 kHz fits a 250 kHz band"
    (unknown,) = meshrf.preset_problems("n1", "WARP_SPEED", us)
    assert "not a preset this tool knows" in unknown
    narrowed = {**us, "band_mhz": [915.0, 915.3]}           # a site that narrows its band
    assert meshrf.preset_problems("n1", "SHORT_TURBO", narrowed)


def test_a_hubs_workers_presets_are_judged_and_a_workers_own_is_not():
    from ansible_collections.rxrbench.core.plugins.module_utils import rfrules
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families
    rules = rfrules.with_region({**rfrules.defaults(), **EU})
    zep = families.plugin("zephyrtastic")
    lora = {"meshtastic": {"config": {"lora": {"modem_preset": "LONG_FAST", "tx_power": 2}}}}
    for block in ({"hub": {"workers": {"head1": "MEDIUM_FAST", "head2": "SHORT_TURBO"}}},
                  {"brain": {"heads": {"head1": "MEDIUM_FAST", "head2": "SHORT_TURBO"}}}):
        (bad,) = zep.rf_check("brain1", {**lora, "zephyrtastic": block}, rules)
        assert "the preset it sets on worker head2 SHORT_TURBO" in bad
    for block in ({"worker": {"hub": "brain1"}}, {"head": {"brain": "brain1"}}):
        head = {"meshtastic": {"config": {"lora": {"modem_preset": "SHORT_TURBO", "tx_power": 2}}},
                "zephyrtastic": block}
        assert zep.rf_check("head2", head, rules) == [], "a worker's preset is its hub's to set"
    assert families.plugin("meshtastic").rf_check("n1", head, rules), "stock judges its own"
