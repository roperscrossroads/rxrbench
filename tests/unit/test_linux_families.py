"""The Linux plugin's three families: what each says of its devices, as data the task files read."""
import pytest

from ansible_collections.rxrbench.core.plugins.plugin_utils import families
from ansible_collections.rxrbench.linux.plugins.module_utils import osinfo

OS_RELEASE = """ID=poky
NAME="Poky (Yocto Project Reference Distro)"
VERSION_ID=5.0.4
PRETTY_NAME="Poky 5.0.4 (scarthgap)"
BUILD_ID="20261010171501"
---
node-1
6.6.63-v8
aarch64
4211
"""


def facts(family, **hostvars):
    return families.plugin(family).board_facts(hostvars, [])


def test_the_three_tiers_differ_only_in_what_they_say():
    assert {f: families.of(f)["update"] for f in ("linux", "linux_rauc", "linux_slotguard")} == {
        "linux": "linux", "linux_rauc": "rauc", "linux_slotguard": "rauc"}
    for f in ("linux", "linux_rauc", "linux_slotguard"):
        row = families.of(f)
        assert (row["vocabulary"], row["locate"], row["radio"], row["console"]) == ("linux", "led", "none", None)
        assert families.owner(f) == "rxrbench.linux"
    # a plain device and a RAUC one name no gate and no landing place; slotguard names both
    assert facts("linux")["health_unit"] is None and facts("linux_rauc")["health_unit"] is None
    assert facts("linux_rauc")["remote_bundle"] is None
    sg = facts("linux_slotguard")
    assert (sg["health_unit"], sg["health_soak"], sg["remote_bundle"]) == (
        "boot-health-check.service", 300, "/data/update.raucb")


def test_a_layouts_own_word_beats_the_familys():
    f = facts("linux_slotguard", bench_rauc_health_unit="my-gate.service",
              bench_rauc_remote_bundle="/var/tmp/u.raucb", bench_led_name="led0",
              bench_host_key_path="/etc/dropbear/key")
    assert (f["health_unit"], f["remote_bundle"], f["led_path"], f["host_key_paths"]) == (
        "my-gate.service", "/var/tmp/u.raucb", "/sys/class/leds/led0", ["/etc/dropbear/key"])
    assert facts("linux_slotguard")["host_key_paths"] == [
        "/data/ssh/ssh_host_ed25519_key", "/data/ssh/ssh_host_ecdsa_key"]
    assert facts("linux", bench_led_path="/run/fake-led")["led_path"] == "/run/fake-led"


def test_every_linux_device_holds_a_host_key_and_can_be_rebooted():
    for f in ("linux", "linux_rauc", "linux_slotguard"):
        assert families.plugin(f).board_secrets({}) == [{"kind": "host_key", "origin": "device", "via": "ssh"}]
        assert families.plugin(f).spec(families.of(f))["reboot"]["uptime"]["command"] == "cat /proc/uptime"
    names = [c["name"] for c in families.plugin("linux_slotguard").diag_commands]
    assert names[:5] == [c["name"] for c in families.plugin("linux").diag_commands]
    assert "rauc-status" in names and "boot-request" in names
    assert "rauc-status" not in [c["name"] for c in families.plugin("linux").diag_commands]


def device(family, **hv):
    return {"firmware": {"family": family, "image": "img"}, **hv}


def problems(family, **hv):
    return families.plugin(family).check_layout({"dev1": device(family, **hv)})


def test_a_rauc_device_must_say_where_a_bundle_lands_and_slotguard_knows():
    assert "where it lands" in problems("linux_rauc", bench_bundle="/x/u.raucb")[0]
    assert problems("linux_rauc", bench_bundle="/x/u.raucb", bench_rauc_remote_bundle="/tmp/u.raucb") == []
    assert problems("linux_rauc") == [], "no bundle declared: nothing to land"
    assert problems("linux_slotguard", bench_bundle="/x/u.raucb") == []


def test_a_wait_shorter_than_the_soak_is_refused():
    assert "soaks for 300 s" in problems("linux_slotguard", bench_bundle="/x/u.raucb", bench_wait_good=120)[0]
    assert problems("linux_slotguard", bench_bundle="/x/u.raucb", bench_wait_good=600) == []
    assert problems("linux_rauc", bench_bundle="/x/u.raucb", bench_rauc_remote_bundle="/t/u",
                    bench_wait_good=5) == [], "no gate, no soak to outwait"
    # one family's check leaves another family's devices alone
    assert families.plugin("linux_rauc").check_layout({"d": device("linux_slotguard", bench_bundle="/x")}) == []


def test_what_a_device_says_it_is_and_where_it_differs_from_what_is_declared():
    f = osinfo.parse(OS_RELEASE)
    assert f["os"]["id"] == "poky" and f["os"]["build_id"] == "20261010171501"
    assert (f["hostname"], f["kernel"], f["machine"], f["uptime_s"]) == ("node-1", "6.6.63-v8", "aarch64", 4211)
    assert osinfo.differs(f["os"], {"id": "poky", "VERSION_ID": "5.0.4"}) == {}
    assert osinfo.differs(f["os"], {"version_id": 5.1, "variant": "x"}) == {
        "version_id": ["5.0.4", "5.1"], "variant": [None, "x"]}
    with pytest.raises(ValueError, match="no os-release part"):
        osinfo.parse("sh: cat: not found")
    with pytest.raises(ValueError, match="cannot read hostname"):
        osinfo.parse("ID=x\n---\nhost\n")
