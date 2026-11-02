"""The card writer's refusals and its handing over to the kit, with the kit, devreg and sudo
replaced. Nothing here touches a block device."""
import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import card, common

READER = {"kind": "block", "destructive": True, "match": {"block_by_id": "usb-x"}}
MANIFEST = {"family": "linux_rauc", "format": "wic", "file": "image.wic", "target": "example-pi"}


class Tools:
    """Stand-ins for devreg, sudo and the kit: records every call."""

    def __init__(self, resolve=(0, "/dev/sdx\n"), guard=(0, "ok"), sudo=(0, ""),
                 kit=(0, "VERIFIED: /dev/sdx matches image.wic")):
        self.answers = {"resolve": resolve, "guard": guard, "sudo": sudo, "kit": kit}
        self.calls = []

    def __call__(self, argv, timeout, env=None, cwd=None):  # pylint: disable=unused-argument
        self.calls.append((argv, env))
        if argv[0] == "sudo":
            return self.answers["sudo"]
        if argv[0] == "devreg":
            return self.answers[argv[1]]
        return self.answers["kit"]


@pytest.fixture
def image(tmp_path):
    (tmp_path / "image.wic").write_bytes(b"\0" * 1024)
    (tmp_path / "image.wic.bmap").write_text("<bmap/>")
    kit = tmp_path / "write-card.sh"
    kit.write_text("#!/bin/sh\n")
    kit.chmod(0o755)
    return str(tmp_path / "image.wic"), str(kit)


def writer(image, tools, **kw):
    file, kit = image
    args = {"confirm": "sdreader", "compatible": "example-pi", "entry": READER}
    args.update(kw)
    entry = args.pop("entry")
    return card.CardWriter("sdreader", entry, kw.pop("manifest", MANIFEST), file, kit,
                           runner=tools, **{k: v for k, v in args.items() if k != "manifest"})


def test_a_confirmed_registered_guarded_card_is_handed_to_the_kit(image):
    tools = Tools()
    w = writer(image, tools)
    assert w.preflight() == [] and w.path == "/dev/sdx"
    w.write()
    kit_call = tools.calls[-1]
    assert kit_call[0] == [image[1], "/dev/sdx", image[0]]
    assert kit_call[1] == {"YES": "1"}, "the kit's typed confirmation is answered by `confirm`"
    assert [c[0][:2] for c in tools.calls[:2]] == [["devreg", "resolve"], ["devreg", "guard"]]


@pytest.mark.parametrize("kw, why", [
    ({"confirm": None}, "not confirmed"),
    ({"confirm": "/dev/sdb"}, "not confirmed"),
    ({"entry": {"kind": "serial", "destructive": True}}, "not a block device in the registry"),
    ({"entry": {"kind": "block"}}, "does not mark sdreader as meant to be overwritten"),
    ({"compatible": "mesh-node-rpi"}, "the image is 'example-pi'; the card is for"),
    ({"manifest": {**MANIFEST, "format": "raucb"}}, "this flasher writes wic"),
])
def test_what_is_refused_never_reaches_the_hardware(image, kw, why):
    tools = Tools()
    refusals = writer(image, tools, **kw).preflight()
    assert any(why in r for r in refusals), refusals
    assert tools.calls == [], "not even devreg is asked about a write already refused"


def test_no_bmap_is_refused(image, tmp_path):
    (tmp_path / "image.wic.bmap").unlink()
    assert any("no .bmap" in r for r in writer(image, Tools()).preflight())


def test_a_reader_that_is_not_plugged_in_or_not_vouched_for_is_refused(image):
    absent = Tools(resolve=(1, "devreg: 'sdreader' is registered but NOT PRESENT"))
    assert "cannot resolve" in writer(image, absent).preflight()[0]
    protected = Tools(guard=(3, "REFUSED: /dev/sdx is a protected disk"))
    w = writer(image, protected)
    assert "devreg guard refuses /dev/sdx" in w.preflight()[0] and w.path is None


def test_without_passwordless_sudo_nothing_is_written(image):
    tools = Tools(sudo=(1, "sudo: a password is required"))
    w = writer(image, tools)
    assert w.preflight() == []
    with pytest.raises(common.FlashError, match="nothing was written.*attended"):
        w.write()
    assert not any(c[0][0] == image[1] for c in tools.calls), "the kit was never started"


def test_a_kit_that_does_not_say_verified_is_a_failure(image):
    tools = Tools(kit=(0, "done."))
    w = writer(image, tools)
    w.preflight()
    with pytest.raises(common.FlashError, match="do NOT boot this card"):
        w.write()


def test_a_plan_makes_every_check_but_the_confirmation(image):
    tools = Tools()
    w = writer(image, tools, confirm=None, planning=True)
    assert w.preflight() == [] and w.path == "/dev/sdx"
    absent = Tools(resolve=(1, "NOT PRESENT"))
    assert "cannot resolve" in writer(image, absent, confirm=None, planning=True).preflight()[0]
