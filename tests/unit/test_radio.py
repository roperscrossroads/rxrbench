import pytest

from ansible_collections.rxrbench.mesh.plugins.module_utils import radio

FRESH = {"modem_preset": "LONG_FAST", "use_preset": True, "live_preset": "LONG_FAST",
         "region": "US", "tx_power": 30, "tx_enabled": True}
WANT = {"region": "US", "modem_preset": "SHORT_FAST", "tx_power": 2, "tx_enabled": True}


def test_a_fresh_board_is_silenced_before_it_is_retuned_and_power_comes_first():
    cmds, d = radio.plan(WANT, FRESH)
    assert cmds == ["meshtastic lora tx off", "meshtastic lora power 2",
                    "meshtastic lora preset ShortFast", "meshtastic lora tx on"]
    assert d == {"modem_preset": ("LONG_FAST", "SHORT_FAST"), "tx_power": (30, 2)}


def test_a_board_that_is_as_declared_gets_no_command():
    done = {**FRESH, "modem_preset": "SHORT_FAST", "live_preset": "SHORT_FAST", "tx_power": 2}
    assert radio.plan(WANT, done) == ([], {})


def test_a_quiet_board_is_retuned_without_ever_being_switched_on_early():
    quiet = {**FRESH, "tx_enabled": False}
    cmds, _ = radio.plan(WANT, quiet)
    assert cmds == ["meshtastic lora power 2", "meshtastic lora preset ShortFast",
                    "meshtastic lora tx on"]
    cmds, _ = radio.plan({**WANT, "tx_enabled": False}, quiet)
    assert cmds == ["meshtastic lora power 2", "meshtastic lora preset ShortFast"]


def test_only_the_transmitter_differs():
    done = {**FRESH, "modem_preset": "SHORT_FAST", "tx_power": 2}
    assert radio.plan({**WANT, "tx_enabled": False}, done)[0] == ["meshtastic lora tx off"]
    assert radio.plan(WANT, {**done, "tx_enabled": False})[0] == ["meshtastic lora tx on"]


def test_an_undeclared_transmitter_is_put_back_as_it_was():
    want = {"modem_preset": "SHORT_FAST", "tx_power": 2}
    assert radio.plan(want, FRESH)[0][-1] == "meshtastic lora tx on"
    assert "meshtastic lora tx on" not in radio.plan(want, {**FRESH, "tx_enabled": False})[0]


def test_what_is_not_declared_is_not_touched():
    cmds, d = radio.plan({"tx_power": 2}, FRESH)
    assert cmds == ["meshtastic lora tx off", "meshtastic lora power 2", "meshtastic lora tx on"]
    assert list(d) == ["tx_power"]


@pytest.mark.parametrize("bad, why", [
    ({"modem_preset": "LONG_TURBO"}, "cannot be set from the shell"),
    ({"modem_preset": "ShortFast"}, "cannot be set from the shell"),
    ({"tx_power": 31}, "0 to 30"),
    ({"tx_power": "2"}, "0 to 30"),
    ({"tx_power": True}, "0 to 30"),
    ({"tx_enabled": "yes"}, "true or false"),
    ({"hop_limit": 3}, "does not manage"),
])
def test_what_the_shell_cannot_be_asked_for_is_refused_before_anything_is_sent(bad, why):
    with pytest.raises(radio.RadioError, match=why):
        radio.plan(bad, FRESH)


def test_a_custom_modem_is_not_replaced_by_a_preset():
    with pytest.raises(radio.RadioError, match="custom modem"):
        radio.plan(WANT, {**FRESH, "use_preset": False})
