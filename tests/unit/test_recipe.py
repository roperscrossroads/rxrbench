"""The catalog must build exactly what the reference recipe strings (tests/fixtures) built."""
import os
import re
import shlex
import tomllib

import pytest
import yaml

from ansible_collections.rxrbench.mesh.plugins.module_utils import recipe


def _workspace():
    """The zephyrtastic workspace on this machine (catalog `sources:`), or a path that is not."""
    from ansible_collections.rxrbench.core.plugins.module_utils import site
    try:
        return site.firmware_source("zephyrtastic")["workspace"]
    except KeyError:
        return "/nonexistent"


HERE = os.path.dirname(__file__)
REPO = os.path.dirname(os.path.dirname(HERE))
CATALOG = yaml.safe_load(open(os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "mesh", "catalog", "zephyrtastic.yml")))["classes"]
OLD = tomllib.load(open(os.path.join(HERE, "..", "fixtures", "fleet-reference.toml"), "rb"))["classes"]
KEY = "/keys/fleet.pem"


def old_defines(build):
    """The -D settings of an old recipe string, as CMake would have received them."""
    argv = shlex.split(build.replace("$HOME/.config/zephyrtastic/fleet-ec-p256.pem", KEY))
    assert argv[:2] == ["west", "build"]
    d = dict(a[2:].split("=", 1) for a in argv if a.startswith("-D"))
    return d, argv[argv.index("-b") + 1]


# Overlays the firmware folded into a base (firmware 0.4.78): prj.conf for every board, the
# sample's board conf for the rest. A recipe no longer lists them, and the image is the same.
FOLDED = {
    "*": {"overlay-rf.conf", "overlay-bench.conf"},
    "xiao_ble/nrf52840": {"overlay-ota-shell.conf", "overlay-ota-ble.conf",
                          "overlay-ota-ble-secure.conf", "overlay-led-status.conf"},
    "heltec_wifi_lora32_v4_r8/esp32s3/procpu": {"overlay-node-id-from-key.conf",
                                                "overlay-led-status.conf"},
    "heltec_vision_master_e290/esp32s3/procpu": {"overlay-node-id-from-key.conf",
                                                 "overlay-led-status.conf"},
}
# Overlays merged into another (the same file but for the class number, now a define).
RENAMED = {"overlay-v4r8-member.conf": "overlay-v4-member.conf"}
# Classes retired since the reference strings: class 7 (the unsigned-cluster XIAO) once XIAOs
# could sign as class 1.
RETIRED = {7}
# Defines a class gained since the reference strings, with why.
ADDED = {
    # Class 1 signs; it fits on the XIAO only with terse asserts.
    1: {"CONFIG_ASSERT_MODULE_DEFAULT_LEVEL_TERSE": "y"},
}


@pytest.mark.parametrize("cid", sorted(int(k) for k in OLD if int(k) not in RETIRED))
def test_catalog_renders_the_old_recipe(cid):
    cls = CATALOG[cid]
    want, board = old_defines(OLD[str(cid)]["build"])
    argv = recipe.render(cid, cls, "/tree", "0.4.38", "build-x", key_file=KEY)
    got = dict(a[2:].split("=", 1) for a in argv if a.startswith("-D"))
    # What the old build scripts added on top of the string, and render() adds itself.
    assert got.pop("CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION") == '"0.4.38+0"'
    assert got.pop("ZEPHYR_EXTRA_MODULES") == "/tree"
    # Added since the reference strings: the class's bundle name, quoted (a Kconfig string).
    assert got.pop("CONFIG_MESHTASTIC_FLEET_BUNDLE") == f'"{cls["name"]}"'
    # Since the catalog became the only copy of the recipes, a class's overlays
    # are allowed to grow: render() must pass the catalog's list, in the catalog's order, and
    # the test below keeps it equal to the firmware's own CI. The old recipe's overlays must
    # all still be there, in their order.
    conf, old_conf = got.pop("EXTRA_CONF_FILE").split(";"), want.pop("EXTRA_CONF_FILE").split(";")
    assert conf == cls["overlays"]
    folded = FOLDED["*"] | FOLDED.get(cls["board"], set())
    old_conf = [RENAMED.get(c, c) for c in old_conf if c not in folded]
    assert not folded & set(conf), "a folded overlay is listed again"
    assert [c for c in conf if c in old_conf] == old_conf
    # Every class states its number and signed TLV in the recipe (since firmware 0.4.78); an old
    # recipe that had them from an overlay or the board conf did not pass them.
    assert got.pop("CONFIG_MESHTASTIC_FLEET_CLASS") == str(cid)
    assert got.pop("CONFIG_MCUBOOT_EXTRA_IMGTOOL_ARGS") == f'"--custom-tlv 0x00A0 0x{cid:02x}"'
    want.pop("CONFIG_MESHTASTIC_FLEET_CLASS", None)
    want.pop("CONFIG_MCUBOOT_EXTRA_IMGTOOL_ARGS", None)
    for k, v in ADDED.get(cid, {}).items():
        assert got.pop(k) == v
    assert got == want
    assert cls["board"] == board == OLD[str(cid)]["board"]
    assert cls["hw_model"] == OLD[str(cid)]["hw_model"]
    assert argv[:9] == ["west", "build", "-d", "build-x", "--sysbuild", "-b", board,
                        "/tree/samples/meshtastic", "--"]


def test_every_class_has_what_other_tools_read():
    """The catalog is the only copy of the class recipes; the firmware's own build tools read
    board, hw_model, build_dir and notes from it."""
    dirs = [cls["build_dir"] for cls in CATALOG.values()]
    assert len(set(dirs)) == len(dirs), "two classes share a build directory"
    for cid, cls in CATALOG.items():
        assert cls.get("build_dir", "").startswith("build-"), cid
        assert isinstance(cls.get("hw_model"), int), cid
        assert cls.get("notes") or cls.get("summary"), cid


def test_every_old_class_is_in_the_catalog():
    """The reference recipes are a migration check: every class they had is still here. A
    class added since (14, the E290) is the catalog's own and has no old recipe to match."""
    assert set(int(k) for k in OLD) - RETIRED <= set(CATALOG)
    assert not RETIRED & set(CATALOG), "a retired class is back in the catalog"


def test_the_brain_feature_appends_its_overlay_once():
    cls = CATALOG[4]
    o = recipe.overlays_for(cls, ["brain", "brain"])
    assert o[-1] == "overlay-attachment-brain.conf" and o.count(o[-1]) == 1
    assert o[:-1] == cls["overlays"]


def test_an_unknown_feature_is_an_error():
    with pytest.raises(recipe.RecipeError):
        recipe.overlays_for(CATALOG[4], ["warp-drive"])


def test_the_version_is_quoted_and_gets_a_build_number():
    assert recipe.sign_version("0.4.38") == "0.4.38+0"
    assert recipe.sign_version("0.4.38+3") == "0.4.38+3"
    for bad in ("0.4", "v0.4.38", "0.4.x", ""):
        with pytest.raises(recipe.RecipeError):
            recipe.sign_version(bad)


def test_a_signed_class_without_a_key_is_refused():
    with pytest.raises(recipe.RecipeError):
        recipe.render(4, CATALOG[4], "/tree", "0.4.38", "b")


def test_the_image_key_follows_what_changes_the_image_and_nothing_else():
    k = recipe.image_key(4, CATALOG[4], "abc123", "0.4.38")
    assert k == recipe.image_key(4, CATALOG[4], "abc123", "0.4.38+0")
    assert k != recipe.image_key(4, CATALOG[4], "abc124", "0.4.38")
    assert k != recipe.image_key(4, CATALOG[4], "abc123", "0.4.39")
    assert k != recipe.image_key(4, CATALOG[4], "abc123", "0.4.38", ["brain"])
    assert k != recipe.image_key(6, CATALOG[6], "abc123", "0.4.38")
    assert len(k) == 16


def test_the_artifact_matches_the_flasher():
    assert recipe.artifact(CATALOG[1])["kind"] == "uf2"
    assert recipe.artifact(CATALOG[4])["kind"] == "bin"


# The firmware's own CI builds every fleet class too: its sample.yaml has a `fleet` scenario
# per class (sample.meshtastic.fleet.c<N>) whose overlay list repeats this catalog's. Two
# copies of one fact drift, so they are compared here whenever the firmware tree is at hand
# (catalog `sources:`); a clone without it skips. RXRBENCH_FIRMWARE_TREE names another tree
# (a firmware worktree whose branch goes with this one's).
SAMPLE = os.path.join(os.environ.get("RXRBENCH_FIRMWARE_TREE")
                      or os.path.join(_workspace(), "main"),
                      "samples", "meshtastic", "sample.yaml")


def _fleet_scenarios():
    if not os.path.isfile(SAMPLE):
        return {}
    tests = yaml.safe_load(open(SAMPLE))["tests"]
    out = {}
    by_name = {cls["name"]: cid for cid, cls in CATALOG.items() if cls.get("name")}
    for name, t in tests.items():
        # A class's scenario is named after its number (fleet.c<N>) or, since the bundle
        # names, after its bundle (fleet.<name>).
        m = re.fullmatch(r"sample\.meshtastic\.fleet\.(?:c(\d+)|([a-z0-9-]+))", name)
        cid = (int(m.group(1)) if m.group(1) else by_name.get(m.group(2))) if m else None
        if cid is not None:
            args = t.get("extra_args") or []
            args = args if isinstance(args, list) else [args]
            d = dict(str(a).split("=", 1) for a in args if "=" in str(a))
            out[cid] = (t, {k.strip(): v.strip().strip('"') for k, v in d.items()})
    return out


@pytest.mark.skipif(not os.path.isfile(SAMPLE), reason="the firmware tree is not here")
@pytest.mark.parametrize("cid", sorted(_fleet_scenarios()) or [0])
def test_the_firmware_ci_builds_each_class_as_the_catalog_says(cid):
    t, args = _fleet_scenarios()[cid]
    cls = CATALOG[cid]
    assert args.get("EXTRA_CONF_FILE", "").split(";") == cls["overlays"], \
        f"sample.meshtastic.fleet.c{cid} and catalog class {cid} list different overlays"
    assert args.get("EXTRA_DTC_OVERLAY_FILE", "") == ";".join(cls.get("dtc_overlays") or [])
    assert args.get("SHIELD") == cls.get("shield")
    assert cls["board"] in t.get("platform_allow", [])


def test_every_class_has_a_unique_bundle_name():
    """Layouts, the firmware and people name a class by its bundle (docs: CLASS-NAMING)."""
    names = [cls.get("name") for cls in CATALOG.values()]
    assert all(n and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", n) for n in names), names
    assert len(set(names)) == len(names), "two classes share a bundle name"


def test_the_bundle_name_reaches_the_build():
    cid, cls = 15, CATALOG[15]
    argv = recipe.render(cid, cls, "/tree", "0.4.38", "build-x", key_file=KEY)
    assert f'-DCONFIG_MESHTASTIC_FLEET_BUNDLE="{cls["name"]}"' in argv
