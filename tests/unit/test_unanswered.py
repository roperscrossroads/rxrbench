"""A board that is not there to read is skipped (`unanswered`), not failed: devreg's word that a
registered device is absent, read before anything is exchanged with it."""
import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common


def devreg_says(monkeypatch, rc, out):
    monkeypatch.setattr(common, "tool", lambda argv, timeout, **kw: (rc, out))


def test_a_registered_device_devreg_does_not_find_is_absent(monkeypatch):
    devreg_says(monkeypatch, common.DEVREG_ABSENT, "'b1' is registered but NOT PRESENT right now")
    port, why = common.guarded_port("devreg.py", "b1")
    assert port is None and isinstance(why, common.Absent) and "NOT PRESENT" in why


@pytest.mark.parametrize("rc", [2, 3, 4])
def test_an_ambiguous_or_refused_lookup_is_not_absence(monkeypatch, rc):
    devreg_says(monkeypatch, rc, "two devices match")
    _port, why = common.guarded_port("devreg.py", "b1")
    assert why and not isinstance(why, common.Absent)


class Absentee(common.Flasher):
    """Answers as another build of its family; its preflight finds no port."""
    def __init__(self, why):
        self.why = why

    def device(self):
        return {"family": "zephyrtastic", "build": "00000000", "class": 6}

    def preflight(self):
        return [self.why]


MANIFEST = {"family": "zephyrtastic", "build": "11111111", "class": 6, "target": "t"}


def test_a_preflight_that_finds_the_board_absent_skips_it():
    with pytest.raises(common.Unanswered, match="NOT PRESENT"):
        common.drive(Absentee(common.Absent("devreg cannot resolve b1: NOT PRESENT")), MANIFEST,
                     check_mode=True)


def test_any_other_preflight_reason_is_a_refusal():
    with pytest.raises(common.FlashError, match="refused: no esptool") as e:
        common.drive(Absentee("no esptool at /x"), MANIFEST, check_mode=True)
    assert not isinstance(e.value, common.Unanswered)


# -- a first flash declared for a board that does not need one (left over) -----------------
class Answers(common.Flasher):
    def __init__(self, build="11111111", used=False):
        self.build, self.used = build, used

    def device(self):
        return {"family": "zephyrtastic", "build": self.build, "class": 6}

    def first_flash_used(self):
        return self.used


@pytest.mark.parametrize("build", ["11111111", "00000000"])
def test_a_first_flash_declared_for_a_board_that_answers_is_flagged_left_over(build):
    r = common.drive(Answers(build), MANIFEST, check_mode=True, first_flash=True)
    assert r["first_flash_unneeded"] is True


def test_no_flag_without_the_declaration_or_when_the_flasher_needed_it():
    assert common.drive(Answers("00000000"), MANIFEST, check_mode=True)["first_flash_unneeded"] is False
    r = common.drive(Answers("00000000", used=True), MANIFEST, check_mode=True, first_flash=True)
    assert r["first_flash_unneeded"] is False


class Silent(common.Flasher):
    def device(self):
        raise common.NotOurs("b1 takes no input on its console")

    def first_flash_refusals(self):
        return []


def test_a_first_flash_that_was_needed_is_not_flagged():
    r = common.drive(Silent(), MANIFEST, check_mode=True, first_flash=True)
    assert r["first"] is True and r["first_flash_unneeded"] is False


class Warned:
    def __init__(self):
        self.said = []

    def warn(self, msg):
        self.said.append(msg)


def test_the_warning_names_the_board_and_the_flag():
    m = Warned()
    common.stale_first_flash(m, "b1", {"first_flash_unneeded": True})
    common.stale_first_flash(m, "b2", {"first_flash_unneeded": False})
    assert len(m.said) == 1 and "b1" in m.said[0] and "bench_first_flash" in m.said[0]
