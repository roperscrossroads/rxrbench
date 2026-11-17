"""A route that delivers an image itself, as core sees it: what a plugin may declare, what a
layout is told or refused, and how a flasher's delivery speaks to the route's tool. The route
and the family that takes it are the fixture plugin's, so this holds with core alone."""
import copy
import os
import stat

import pytest
import yaml

from ansible_collections.rxrbench.core.plugins.module_utils import layout
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, delivery
from ansible_collections.rxrbench.core.plugins.plugin_utils import families, registry

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures")
REG = layout.load_registry(os.path.join(FIX, "registry.yaml"))
COURIER = {"host": "benchA", "addr": "member1", "state": "live", "can": ["flash"]}


# --- what a plugin may declare ---------------------------------------------------------------

def test_a_plugin_brings_a_delivering_route_as_data(widgets):
    assert widgets.route_delivery("courier") == {
        "bearer": "courier", "tool": "courier_route.py", "formats": ["widget-image"],
        "settings": "bench_courier"}
    assert widgets.route_delivery("usb") is None and widgets.route_delivery("nowhere") is None


def test_a_family_takes_no_delivery_unless_it_says_so(widgets):
    assert widgets.plugin("widget").takes_delivery == ("widget-image",)
    assert widgets.plugin("beacon").takes_delivery == ()
    assert widgets.plugin("zephyr_mcumgr").takes_delivery == ()


def test_a_delivering_route_may_not_take_the_name_of_one_core_has(tmp_path):
    def plugin(name, body):
        d = tmp_path / "ansible_collections" / "fixture" / name / "plugins" / "plugin_utils"
        d.mkdir(parents=True)
        (d / "bench.py").write_text(body)
    plugin("shadow", 'FAMILIES = {}\nROUTE_DELIVERIES = {"ssh": {"tool": "t.py", "formats": ("x",)}}\n')
    plugin("vague", 'FAMILIES = {}\nROUTE_DELIVERIES = {"courier": {"formats": ("x",)}}\n')
    try:
        with pytest.raises(registry.PluginError, match="already known"):
            families.load(["fixture.shadow"], roots=[str(tmp_path)])
        with pytest.raises(registry.PluginError, match="names no tool"):
            families.load(["fixture.vague"], roots=[str(tmp_path)])
    finally:
        families.load()


# --- what a layout is told, or refused ---------------------------------------------------------

def resolved(tmp_path, hostvars, reach):
    reg = copy.deepcopy(REG)
    reg["devices"]["member1"]["reach"] = {"usb": {"host": "benchA"}, **reach}
    hv = tmp_path / "host_vars"
    hv.mkdir(exist_ok=True)
    (hv / "member1.yml").write_text(yaml.safe_dump(hostvars))
    return layout.load_layout(str(tmp_path), {"boards": {"member1": {"host": "benchA"}},
                                              "catalog_dir": os.path.join(FIX, "catalog")}, reg)


WIDGET = {"firmware": {"family": "widget", "image": "widget-heltec-v4"}}
BEACON = {"firmware": {"family": "beacon", "image": "beacon-heltec-v4r8"},
          "beacon": {"radio": {"tx": False}}}


def test_a_pinned_delivering_route_reaches_the_update_step_as_data(tmp_path, widgets):
    v = resolved(tmp_path, {**WIDGET, "routes": {"flash": "courier"}},
                 {"courier": COURIER})["boards"]["member1"]["vars"]
    assert (v["bench_routes"]["flash"]["bearer"], v["bench_routes"]["flash"]["addr"]) == ("courier", "member1")
    assert v["bench_routes"]["shell"]["bearer"] == "usb"         # it is read as before
    assert v["bench_delivery"] == widgets.route_delivery("courier")


def test_a_delivering_route_is_not_chosen_while_another_route_can_flash(tmp_path, widgets):
    v = resolved(tmp_path, WIDGET, {"courier": COURIER})["boards"]["member1"]["vars"]
    assert v["bench_routes"]["flash"]["bearer"] == "usb" and "bench_delivery" not in v


def test_a_family_that_cannot_take_a_delivery_is_refused_with_the_reason(tmp_path, widgets):
    with pytest.raises(layout.LayoutError, match="delivers an image itself.*'beacon' cannot take a delivered image"):
        resolved(tmp_path, {**BEACON, "routes": {"flash": "courier"}}, {"courier": COURIER})


def test_a_pin_the_registry_does_not_list_or_that_is_not_live_is_refused(tmp_path, widgets):
    with pytest.raises(layout.LayoutError, match="pinned to courier, which the registry does not list"):
        resolved(tmp_path, {**WIDGET, "routes": {"flash": "courier"}}, {})
    with pytest.raises(layout.LayoutError, match="pinned to courier, which is unproven"):
        resolved(tmp_path, {**WIDGET, "routes": {"flash": "courier"}},
                 {"courier": {k: v for k, v in COURIER.items() if k != "state"}})


# --- a flasher's side of a delivery --------------------------------------------------------------

def stub_tool(tmp_path, body):
    p = tmp_path / "tool.py"
    p.write_text("import json, sys\nreq = json.load(sys.stdin)\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return {"bearer": "courier", "tool": str(p), "formats": ["raucb"], "settings": {"key": "made-up"}}


def test_the_tool_is_given_the_settings_on_stdin_and_never_on_its_command_line(tmp_path):
    spec = stub_tool(tmp_path, "print(json.dumps({'argv': sys.argv[1:], 'saw': req}))\n")
    out = delivery.Delivery(spec, "dev1").state()
    assert out["argv"] == ["state"]
    assert out["saw"] == {"settings": {"key": "made-up"}, "target": "dev1"}


def test_a_tool_that_refuses_is_a_refusal_with_its_own_words(tmp_path):
    spec = stub_tool(tmp_path, "print(json.dumps({'error': 'the server is closed on Sundays'}))\nsys.exit(1)\n")
    with pytest.raises(common.FlashError, match="courier: state for dev1: the server is closed on Sundays"):
        delivery.Delivery(spec, "dev1").state()


def test_a_tool_that_is_not_on_the_host_is_said_so(tmp_path):
    with pytest.raises(common.FlashError, match="is not on this host"):
        delivery.Delivery({"bearer": "courier", "tool": "absent.py", "formats": ["raucb"]}, "dev1",
                          bin_dir=str(tmp_path)).state()


def test_a_route_refuses_an_image_it_does_not_carry_and_a_device_with_no_address(tmp_path):
    spec = stub_tool(tmp_path, "")
    assert "carries raucb" in delivery.Delivery(spec, "dev1").refusals({"format": "uf2"})[0]
    assert "no address" in delivery.Delivery(spec, None).refusals({"format": "raucb"})[0]
    assert delivery.Delivery(spec, "dev1").refusals({"format": "raucb"}) == []
