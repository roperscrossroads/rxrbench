"""Which depth a read takes (docs/ARCHITECTURE.md#checking-depth)."""
import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import depth


def test_full_is_the_default_and_full_asked_is_full():
    assert depth.resolve() == {"depth": "full", "reason": "asked"}
    assert depth.resolve("full", read="before write", spot_reads=("before write",))["depth"] == "full"


def test_spot_is_taken_only_where_the_read_has_a_spot_form():
    assert depth.resolve("spot", read="before write", spot_reads=("before write",)) == \
        {"depth": "spot", "reason": "asked"}
    got = depth.resolve("spot", read="before write", spot_reads=())
    assert got == {"depth": "full", "reason": "before write has no spot form"}


@pytest.mark.parametrize("moment", sorted(depth.FORCED))
def test_every_forcing_moment_reads_in_full_and_says_why(moment):
    got = depth.resolve("spot", read="r", spot_reads=("r",), **{moment: True})
    assert got == {"depth": "full", "reason": depth.FORCED[moment]}


def test_a_depth_or_a_moment_it_does_not_know_is_an_error():
    with pytest.raises(ValueError):
        depth.resolve("quick")
    with pytest.raises(ValueError):
        depth.resolve("spot", tired=True)
