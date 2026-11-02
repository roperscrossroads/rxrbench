"""What an image transmits before its config is written, against the site's RF rules."""
import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import rflayout, rfrules
from ansible_collections.rxrbench.core.plugins.plugin_utils import families

families.specs()   # the radio plugins register their regions' bands as they load

EU_DEFAULTS = {"boot_radio": {"freq_mhz": 869.618, "bw_khz": 62.5, "tx_dbm": 22}}


@pytest.fixture
def lora_bands(monkeypatch):
    """A plugin's bands for one radio (rfrules.register_radio), as a radio plugin brings them."""
    monkeypatch.setitem(rfrules.RADIOS, "lora", {"US": {"band_mhz": [902.0, 928.0], "max_tx_power": 30},
                                                 "EU": {"band_mhz": [869.4, 869.65], "max_tx_power": 27}})


def test_an_image_that_boots_outside_the_band_is_named_with_why(lora_bands):
    p = rflayout.boot_radio("n1", EU_DEFAULTS, {"region": "US"})
    assert p and not any(x.startswith("n1: ") for x in p) and any("outside the band" in x for x in p)


def test_the_power_ceiling_holds_for_the_boot_radio_too():
    img = {"boot_radio": [{"freq_mhz": 915.0, "bw_khz": 250, "tx_dbm": 22}]}
    assert rflayout.boot_radio("n1", img, {"region": "US"}) == []
    assert any("ceiling" in x for x in rflayout.boot_radio("n1", img, {"region": "US", "max_tx_power": 2}))


def test_an_image_with_no_boot_radio_recorded_says_nothing():
    assert rflayout.boot_radio("n1", {}, {"region": "US"}) == []
    assert rflayout.boot_radio("n1", None, {"region": "US"}) == []
