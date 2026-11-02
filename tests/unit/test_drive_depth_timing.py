"""drive(): the depth of the checks before a write, and the time of each phase
(docs/ARCHITECTURE.md#checking-depth)."""
from ansible_collections.rxrbench.core.plugins.module_utils import identity
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common

MANIFEST = {"family": "bare", "hash": "b" * 64}


class Fake(common.Flasher):
    def __init__(self, runs, spot_reads=()):
        self.runs, self.spot_reads, self.seen = runs, spot_reads, None

    def device(self):
        return identity.make("bare", hash=self.runs)

    def preflight(self):
        self.seen = dict(self.checked)
        return []

    def write(self):
        self.runs = MANIFEST["hash"]

    def verify(self):
        return self.device()


def test_a_board_on_its_image_is_read_timed_and_checked_in_full():
    r = common.drive(Fake(MANIFEST["hash"]), MANIFEST)
    assert r["changed"] is False and r["checked"] == {"depth": "full", "reason": "asked"}
    assert [p["phase"] for p in r["timing"]] == ["read", "leave"]


def test_a_write_times_each_phase_and_preflight_sees_the_depth():
    f = Fake("a" * 64, spot_reads=("before write",))
    r = common.drive(f, MANIFEST, check_depth="spot")
    assert r["changed"] is True and f.seen == {"depth": "spot", "reason": "asked"}
    assert [p["phase"] for p in r["timing"]] == ["read", "preflight", "write", "verify", "leave"]


def test_spot_on_a_flasher_with_no_spot_form_reads_in_full():
    f = Fake("a" * 64)
    r = common.drive(f, MANIFEST, check_depth="spot", check_mode=True)
    assert r["checked"] == {"depth": "full", "reason": "before write has no spot form"}
