"""The launcher family as the layout and the survey see it: its row, what it refuses in a
`firmware:` declaration, what it makes of a board's `launcher:` block, and what it says of
a probe."""
from ansible_collections.rxrbench.launcher.plugins.plugin_utils import bench
from ansible_collections.rxrbench.launcher.plugins.plugin_utils.families import launcher

FAMILY = bench.PLUGINS["launcher"]
CATALOG = {"images": {
    "launcher-cyd-2.9.1": {"family": "launcher", "kind": "manager", "version": "2.9.1",
                           "board_tags": ["cyd"], "flasher": "launcher",
                           "source": {"type": "path", "path": "/x/Launcher.bin"}},
    "cam-stream": {"family": "launcher", "kind": "app", "board_tags": ["cyd"], "flasher": "launcher",
                   "source": {"type": "path", "path": "/x/cam-stream.bin"}},
    "mesh-thing": {"family": "mesh", "board_tags": ["x"], "flasher": "esptool",
                   "source": {"type": "path", "path": "/x/m.bin"}},
}}


def test_the_row_and_the_plugin_agree():
    row = bench.FAMILIES["launcher"]
    assert isinstance(FAMILY, launcher.Family)
    assert row["console"] == "launcher" and row["update"] == "launcher" and row["radio"] == "none"
    assert row["identity_key"] is None and row["plugin"] == "launcher"
    # Launcher has no state line: its identity is the `version` command's answer
    assert FAMILY.state["fallback"]["command"] == "version"
    assert [d["name"] for d in FAMILY.diag_commands] == ["version", "partitions"]


def test_a_firmware_declaration_must_name_a_launcher_image_not_an_app():
    rec, problems = FAMILY.image({"family": "launcher", "image": "launcher-cyd-2.9.1"}, CATALOG)
    assert rec == {"board_tags": ["cyd"], "flasher": "launcher"} and problems == []
    rec, problems = FAMILY.image({"family": "launcher", "image": "cam-stream"}, CATALOG)
    assert rec is not None and problems == [
        "image cam-stream is an app Launcher installs, not a Launcher image: declare it under launcher.apps"]
    _rec, problems = FAMILY.image({"family": "launcher", "image": "mesh-thing"}, CATALOG)
    assert problems == ["image mesh-thing is mesh firmware, the board declares launcher"]
    rec, problems = FAMILY.image({"family": "launcher", "image": "nope"}, CATALOG)
    assert rec is None and problems == ["image 'nope' is not in the firmware catalog"]


def test_the_secrets_a_board_declares_follow_its_block():
    assert [s["kind"] for s in FAMILY.board_secrets({"launcher": {"apps": {}}})] == ["wui_password"]
    both = FAMILY.board_secrets({"launcher": {"wifi": [{"ssid": "lab-iot"}]}})
    assert [s["kind"] for s in both] == ["wui_password", "wifi_psk"]
    assert all(s["origin"] == "declared" for s in both)
    assert [s["kind"] for s in FAMILY.board_secrets({})] == ["wui_password"]


def test_the_board_facts_come_from_the_block_with_their_defaults():
    assert FAMILY.board_facts({}, {}) == {"launcher_apps": {}, "launcher_boot": None, "launcher_prune": False}
    hv = {"launcher": {"apps": {"camstr": {"image": "cam-stream"}}, "boot": "camstr", "prune": True}}
    assert FAMILY.board_facts(hv, {}) == {"launcher_apps": {"camstr": {"image": "cam-stream"}},
                                          "launcher_boot": "camstr", "launcher_prune": True}


def test_check_layout_names_every_shape_launcher_cannot_take():
    fw = {"family": "launcher", "image": "launcher-cyd-2.9.1"}
    good = {"cyd1": {"firmware": fw, "launcher": {
        "apps": {"camstr": {"image": "cam-stream"}, "wifi01": {"image": "wifi-test"}},
        "boot": "camstr", "wifi": [{"ssid": "lab-iot"}]}}}
    assert FAMILY.check_layout(good) == []
    bad = {"cyd1": {"firmware": fw, "launcher": {
        "apps": {"camstream": {"image": "cam-stream"},      # nine letters: Launcher keeps six
                 "wi-fi": {"image": "wifi-test"},           # not letters and digits
                 "buddy": {"image": "buddy"},               # five: Launcher pads to buddy0
                 "noimg0": {},                              # names no image
                 "plain0": "cam-stream"},                   # not a block
        "boot": "nothere", "wifi": [{"ssid": "ok"}, {"pwd": "x"}, "lab-iot"]}},
        "mesh1": {"firmware": {"family": "mesh", "image": "x"}, "launcher": {"boot": "ignored"}}}
    out = FAMILY.check_layout(bad)
    rule = "a slot label is 6 lowercase letters and digits, what Launcher makes of an app's name"
    assert out == [
        f"cyd1: launcher.apps.camstream: {rule} (of 'camstream': 'camstr')",
        f"cyd1: launcher.apps.wi-fi: {rule} (of 'wi-fi': 'wifi00')",
        f"cyd1: launcher.apps.buddy: {rule} (of 'buddy': 'buddy0')",
        "cyd1: launcher.apps.noimg0 names no image",
        "cyd1: launcher.apps.plain0 names no image",
        "cyd1: launcher.boot is 'nothere', which launcher.apps does not declare",
        "cyd1: launcher.wifi[1] names no ssid",
        "cyd1: launcher.wifi[2] names no ssid",
    ], "a board of another family is not looked at"


def test_what_the_survey_says_of_a_launcher_probe():
    assert FAMILY.survey_tags({"version": "2.9.1"}) == {"fw:launcher", "launcher:2.9.1"}
    assert FAMILY.survey_tags({}) == {"fw:launcher"}
    assert FAMILY.survey_describe({"version": "beta-60a40c8"}) == "Launcher beta-60a40c8"
    assert FAMILY.survey_describe({}) == "Launcher ?"
    assert FAMILY.enroll_note({"version": "2.9.1"}) == {"delivered": "Launcher 2.9.1"}
    assert FAMILY.enroll_note({}) == {}


def test_a_slot_label_is_what_launcher_makes_of_a_name():
    from ansible_collections.rxrbench.launcher.plugins.module_utils import images
    assert images.slot_label("buddy") == "buddy0" and images.slot_label("cam-stream") == "camstr"
    assert images.slot_label("M9-MUI") == "m9mui0" and images.slot_label("wifi-test") == "wifite"
    assert images.slot_label("---") == "app000" and images.slot_label("Launcher-2.9.1") == "launch"
    assert images.next_slot_label("buddy", {"buddy0"}) == "buddy1"
    assert images.next_slot_label("buddy", {"buddy0"} | {f"buddy{i}" for i in range(1, 10)}) == "app1"


def _surveyed(images, boot_seq):
    """The survey's record of a 4 MB chip under Launcher with two apps (camstr, wifite)."""
    import importlib.util
    import os
    from ansible_collections.rxrbench.core.plugins.module_utils import esppart
    from tests.unit import esptables
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location("usb_survey", os.path.join(
        repo, "collections", "ansible_collections", "rxrbench", "core", "plugins", "modules", "usb_survey.py"))
    survey = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(survey)
    rows = list(esptables.LAUNCHER_4MB) + [esptables.row(0, 0x10, 0x160000, 0x100000, "camstr"),
                                           esptables.row(0, 0x11, 0x260000, 0xE0000, "wifite")]
    blob = esptables.chip(rows, images=images, boot_seq=boot_seq)
    parsed = esppart.parse(blob[0x8000:0x8C00])
    rec = survey.survey_inventory(parsed, lambda o, n: blob[o:o + n])
    return {"manager": esppart.manager(parsed), **rec}


def test_a_launcher_layout_owns_launcher_its_declared_apps_and_then_the_shared_data():
    parts = _surveyed([0x10000, 0x160000, 0x260000], boot_seq=1)
    both = FAMILY.chip_regions({"launcher": {"apps": {"camstr": {}, "wifite": {}}}}, parts)
    assert sorted(both["owned"]) == ["camstr", "nvs", "otadata", "test", "wifite"]
    assert both["describe"] == "Launcher in test at 0x10000 with apps: camstr, wifite"
    # wifite is not declared: it stays other firmware, and the data it shares is not the layout's
    one = FAMILY.chip_regions({"launcher": {"apps": {"camstr": {}}}}, parts)
    assert sorted(one["owned"]) == ["camstr", "test"]
    # an empty slot holds nothing to share data with
    empty = FAMILY.chip_regions({"launcher": {"apps": {"camstr": {}}}},
                                _surveyed([0x10000, 0x160000], boot_seq=1))
    assert sorted(empty["owned"]) == ["camstr", "nvs", "otadata", "test"]
    assert empty["describe"].endswith("with apps: camstr")
    assert FAMILY.chip_regions({}, {"manager": None}) == {"owned": [], "describe": ""}


def test_the_plan_calls_nothing_foreign_on_a_launcher_board_that_declares_it_all():
    from ansible_collections.rxrbench.core.plugins.filter.chip import chip_findings
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families
    parts = _surveyed([0x10000, 0x160000, 0x260000], boot_seq=1)
    facts = {"probes": {"esptool": {"gathered": "2026-11-07T19:25:51Z", "partitions": parts}}}
    families.load(["rxrbench.launcher"])
    try:
        board = {"firmware": {"family": "launcher"}, "launcher": {"apps": {"camstr": {}, "wifite": {}}}}
        assert chip_findings(facts, [], board) == []
        board["launcher"]["apps"] = {"camstr": {}}
        assert chip_findings(facts, [], board) == [
            "the chip carries Launcher in test at 0x10000 with apps: camstr, wifite",
            "nvs at 0x9000 holds data every app on this chip shares (it has an app manager), "
            "as surveyed 2026-11-07T19:25:51Z",
            "otadata at 0xe000 holds data every app on this chip shares (it has an app manager), "
            "as surveyed 2026-11-07T19:25:51Z",
            "wifite at 0x260000 holds other firmware (esp-app-image), as surveyed 2026-11-07T19:25:51Z"]
    finally:
        families.load()
