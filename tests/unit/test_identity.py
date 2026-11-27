"""The inputs here are what real devices and images answered (trimmed)."""
import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import familyspec, identity
from ansible_collections.rxrbench.linux.plugins.module_utils import rauc

HASH = "e81cd5bd6945e36190139efc7e7828bca7db776e8a5c58273daa1d719af714d9"
SMP = [{"slot": 0, "version": "0.4.39", "hash": HASH, "active": True, "confirmed": True,
        "pending": False, "bootable": True}]
IMAGE = {"version": "0.4.39+0", "class": 1, "sha256": HASH, "size": 696913}
STATE = {"build": "9aea7c0a", "image": "0.4.39+0", "class": 1, "board": "xiao_ble"}

BUNDLE = {"compatible": "example-pi", "version": "1.0-20260922102855", "format": "verity",
          "hash": "0669ba0e0516884b3e9ca3742d9c86bc4b971a3b689aea1a1419a661114de152"}
RAUC = {"compatible": "example-pi", "booted": "B", "slots": [
    {"rootfs.1": {"device": "/dev/mmcblk0p3", "bootname": "B", "state": "booted",
                  "boot_status": "good", "slot_status": {"bundle": {
                      "compatible": "example-pi", "version": "1.0-20260922102855",
                      "hash": BUNDLE["hash"]}}}},
    {"rootfs.0": {"device": "/dev/mmcblk0p2", "bootname": "A", "state": "inactive",
                  "boot_status": "good", "slot_status": {"bundle": {
                      "compatible": "example-pi", "version": "1.0-20260922021855",
                      "hash": "af5accef011f875b4fa2b9cebad01f0ab700c85a51783f640e508f0860e175a6"}}}}]}


def of_state(line, family="zephyrtastic"):
    """The identity a family's state line gives (its JSON, as the board printed it)."""
    return identity.make(family, **familyspec.fields_of_line(familyspec.of(family)["identity"], line))



def test_a_board_read_over_mcumgr_is_the_image_it_was_flashed_with():
    dev, flags = identity.of_smp(SMP, family="zephyrtastic")
    assert identity.same(dev, identity.of_mcuboot_image(IMAGE, "zephyrtastic")) is True
    assert flags == {"slot": 0, "confirmed": True, "pending": False}


def test_a_build_counter_reads_the_same_over_mcumgr_as_in_the_image():
    # Zephyr's image manager prints the counter after a fourth dot (a V4-R8 on hardware);
    # MCUboot and the manifest say +45. Only a counter of 0 ever reached the sims.
    dev, _ = identity.of_smp([{**SMP[0], "version": "0.4.46.45"}], family="zephyrtastic")
    image = identity.of_mcuboot_image({**IMAGE, "version": "0.4.46", "build": 45}, "zephyrtastic")
    assert dev["version"] == "0.4.46+45"
    assert identity.same(dev, image) is True
    assert identity.smp_version("0.4.46.0") == "0.4.46"
    assert identity.smp_version("1.2.3") == "1.2.3"


def test_another_image_of_the_same_version_is_not_the_same():
    other = identity.of_mcuboot_image({**IMAGE, "sha256": "00" * 32}, "zephyrtastic")
    dev, _ = identity.of_smp(SMP, family="zephyrtastic")
    assert identity.same(dev, other) is False
    assert list(identity.differences(dev, other)) == ["hash"]


def test_a_board_read_over_its_shell_cannot_be_matched_to_a_file_by_version_alone():
    """The state line has a build id and no hash; an image file has a hash and no build id.
    Two builds can carry one version: equal versions prove nothing."""
    dev = of_state(STATE, "zephyrtastic")
    assert identity.same(dev, identity.of_mcuboot_image(IMAGE, "zephyrtastic")) is None
    # with the build id the image was built from (its manifest), it can
    built = {**identity.of_mcuboot_image(IMAGE, "zephyrtastic"), "build": "9aea7c0a", "target": "xiao_ble"}
    assert identity.same(dev, built) is True
    assert identity.same(dev, {**built, "build": "8d696e6b"}) is False
    assert identity.same(dev, {**built, "class": 7}) is False


def test_a_pi_s_booted_slot_is_the_bundle_it_was_updated_with():
    dev, slot = rauc.of_status(RAUC)
    assert identity.same(dev, rauc.of_bundle(BUNDLE)) is True
    assert slot == {"bootname": "B", "boot_status": "good", "device": "/dev/mmcblk0p3"}
    older = {**BUNDLE, "version": "1.0-20260922021855", "hash": "af" * 32}
    assert identity.same(dev, rauc.of_bundle(older)) is False


def test_a_bundle_for_another_machine_is_never_the_same():
    dev, _ = rauc.of_status(RAUC)
    assert identity.same(dev, rauc.of_bundle({**BUNDLE, "compatible": "mesh-node-rpi"})) is False


def test_what_is_running_must_be_unambiguous():
    with pytest.raises(ValueError, match="active"):
        identity.of_smp([{**SMP[0], "active": False}])
    with pytest.raises(ValueError, match="booted"):
        rauc.of_status({"compatible": "x", "slots": []})


def test_an_image_on_trial_is_reported_as_such():
    _, flags = identity.of_smp([{**SMP[0], "confirmed": False}])
    assert flags["confirmed"] is False


def test_unknown_fields_are_refused():
    with pytest.raises(ValueError):
        identity.make("zephyrtastic", colour="red")


def test_a_hub_and_a_plain_member_of_one_class_and_build_are_different_images():
    """Same class, same build id, same version: only the recipe feature tells them apart."""
    from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
    state = {"board": "b", "image": "0.4.42+0", "build": "347cacb2", "class": 6,
             "feat": ["hub", "cluster", "ble_peer", "phoneapi"]}
    hub = of_state(state, "zephyrtastic")
    assert hub["features"] == ["hub"]
    manifest = {"family": "zephyrtastic", "format": "esp-parts", "file": "f", "class": 6,
                "version": "0.4.42", "build": "347cacb2"}
    plain_image = common.image_identity(manifest)
    assert plain_image["features"] == []
    assert identity.same(hub, plain_image) is False
    assert identity.differences(hub, plain_image) == {"features": (["hub"], [])}
    assert identity.same(hub, common.image_identity({**manifest, "features": ["hub"]})) is True
    # firmware that does not list features is not compared on them
    old = of_state({k: v for k, v in state.items() if k != "feat"}, "zephyrtastic")
    assert identity.same(old, plain_image) is True


def test_a_feature_under_its_former_name_is_the_same_feature():
    """A board built before the rename says brain; an image staged then lists brain: both
    are the hub feature, on either side."""
    from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
    state = {"board": "b", "image": "0.4.42+0", "build": "347cacb2", "class": 6,
             "feat": ["brain", "cluster"]}
    old_board = of_state(state, "zephyrtastic")
    assert old_board["features"] == ["hub"]
    manifest = {"family": "zephyrtastic", "format": "esp-parts", "file": "f", "class": 6,
                "version": "0.4.42", "build": "347cacb2"}
    for staged in (["brain"], ["hub"]):
        image = common.image_identity({**manifest, "features": staged})
        assert image["features"] == ["hub"]
        assert identity.same(old_board, image) is True
        assert identity.same(of_state({**state, "feat": ["hub"]}, "zephyrtastic"), image) is True


def test_an_images_build_counter_is_part_of_its_version_as_a_board_reports_it():
    """imginfo keeps the counter apart; a board running the image says 0.4.46+45."""
    info = {"version": "0.4.46", "build": 45, "class": 1, "sha256": HASH}
    assert identity.mcuboot_version(info) == "0.4.46+45"
    assert identity.mcuboot_version({**info, "build": 0}) == "0.4.46"
    assert identity.mcuboot_version({"version": "0.4.39+0"}) == "0.4.39"
    dev = of_state({"build": "86ea7c42", "image": "0.4.46+45", "class": 1,
                             "board": "xiao_ble"}, "zephyrtastic")
    built = {**identity.of_mcuboot_image(info, "zephyrtastic"), "build": "86ea7c42"}
    assert identity.differences(dev, built) == {}


def test_a_familys_own_identity_field_exists_because_the_family_says_so():
    """Core names no such field. With the families loaded, each one's key is an identity field;
    with none loaded, only the fields any device has are."""
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families
    keys = {row["identity_key"] for row in families.FAMILIES.values() if row["identity_key"]}
    assert keys and set(identity.fields()) == set(identity.BASE) | keys
    assert identity.of_mcuboot_image({**IMAGE}, "zephyr_mcumgr").get("class") is None, \
        "a family with no key takes none from an image"
    familyspec.load({})
    try:
        assert identity.fields() == identity.BASE
        with pytest.raises(ValueError, match="not identity fields"):
            identity.make("anything", **{sorted(keys)[0]: 1})
    finally:
        familyspec.load(families.specs())
