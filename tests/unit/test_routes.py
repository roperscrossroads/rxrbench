import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import routes

# The shape of real registry entries (a Heltec, a XIAO), trimmed.
HELTEC = {"reach": {
    "usb": {"host": "bench-a", "confirmed": "2020-01-01", "can": ["flash", "shell", "config", "diag"],
            "modes": {"app": {"usb": "303a:1001"}, "rom_download": {"can": ["flash"]}}},
    "ble": {"addr": "02:00:00:00:00:26", "hosts": ["bench-a"], "state": "live",
            "can": ["flash", "shell", "config", "diag"]},
    "lora": {"state": "live", "node": "0x1a2a3a4a", "can": ["config", "diag"]},
    "wifi": {"ip": "192.0.2.9", "state": "dormant", "can": ["flash", "shell", "diag"]},
}}
XIAO = {"reach": {
    "usb": {"host": "bench-a", "tool": "uf2_flash", "modes": {
        "app": {"can": ["shell", "config", "diag"]}, "bootloader": {"can": ["flash"]}}},
    "ble": {"addr": "02:00:00:00:00:6F", "hosts": ["bench-b", "bench-a"], "state": "unproven",
            "can": ["flash", "shell"]},
}}


def test_usb_is_the_default_for_everything_it_can_do():
    r = routes.select(HELTEC)
    assert {v: r[v]["bearer"] for v in r} == {v: "usb" for v in routes.VERBS}
    assert r["flash"]["host"] == "bench-a"


def test_a_usb_route_can_do_what_any_of_its_modes_can():
    r = routes.select(XIAO)
    assert r["flash"]["bearer"] == "usb" and r["shell"]["bearer"] == "usb"
    assert r["flash"]["tool"] == "uf2_flash"


def test_a_pin_moves_one_verb_and_carries_the_route_s_address():
    r = routes.select(HELTEC, {"flash": "ble"})
    assert r["flash"]["bearer"] == "ble" and r["flash"]["addr"] == "02:00:00:00:00:26"
    assert r["shell"]["bearer"] == "usb"


@pytest.mark.parametrize("entry, pins, why", [
    (HELTEC, {"flash": "wifi"}, "dormant, not live"),
    (XIAO, {"flash": "ble"}, "unproven, not live"),
    (HELTEC, {"flash": "lora"}, "can only config, diag"),
    (HELTEC, {"flash": "ssh"}, "does not list"),
    (HELTEC, {"reboot": "usb"}, "unknown verb"),
])
def test_a_pin_the_registry_cannot_honour_refuses(entry, pins, why):
    with pytest.raises(routes.RouteError, match=why):
        routes.select(entry, pins)


def test_a_route_that_is_not_live_is_never_chosen_by_default():
    only_ble = {"reach": {"ble": XIAO["reach"]["ble"]}}
    assert routes.select(only_ble) == {}, "unproven: a tool must not try it by itself"


def test_candidates_are_ordered_so_a_fallback_can_be_added_later():
    c = routes.candidates(HELTEC, "config")
    assert [r["bearer"] for r in c] == ["usb", "ble", "lora"]


def test_a_device_with_no_reach_block_has_one_route_that_does_everything():
    r = routes.select({}, default_host="simhost")
    assert all(r[v]["bearer"] == "usb" and r[v]["host"] == "simhost" for v in routes.VERBS)
    assert r["flash"]["synthetic"] is True


def test_a_network_device_is_reached_over_ssh():
    pi = {"reach": {"ssh": {"host": "workstation", "ip": "192.0.2.149", "user": "root", "port": 22,
                            "state": "live", "can": ["flash", "shell", "diag"]}}}
    r = routes.select(pi)
    assert r["flash"]["bearer"] == "ssh" and r["flash"]["ip"] == "192.0.2.149"
    assert r["flash"]["port"] == 22 and "config" not in r


def test_a_usb_entry_that_only_names_its_host_is_taken_to_do_everything_and_says_so():
    r = routes.select({"reach": {"usb": {"host": "bench-a"}}})
    assert set(r) == set(routes.VERBS)
    assert r["flash"]["assumed"] is True and r["flash"]["host"] == "bench-a"
    # a wireless route gets no such benefit of the doubt
    assert routes.select({"reach": {"ble": {"addr": "AA", "state": "live"}}}) == {}


def test_a_wired_network_route_is_a_bearer_with_an_address_and_a_port():
    entry = {"reach": {"swd": {"host": "h", "state": "live", "can": ["flash", "diag"]},
                       "eth": {"host": "h", "ip": "192.0.2.190", "port": 1337, "state": "live",
                               "can": ["flash"]}}}
    assert routes.select(entry)["flash"]["bearer"] == "swd", "the probe first by default"
    pinned = routes.select(entry, {"flash": "eth"})["flash"]
    assert pinned["bearer"] == "eth" and pinned["ip"] == "192.0.2.190" and pinned["port"] == 1337
