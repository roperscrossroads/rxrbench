"""A route that delivers an image itself, as core sees it: what a plugin may declare, what a
layout is told or refused, and how a flasher's delivery speaks to the route's tool. The route
and the family that takes it are the fixture plugin's, so this holds with core alone."""
import copy
import os
import stat

import pytest
import yaml

from ansible_collections.rxrbench.core.plugins.module_utils import layout, routes
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, delivery
from ansible_collections.rxrbench.core.plugins.plugin_utils import families, registry

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures")
REG = layout.load_registry(os.path.join(FIX, "registry.yaml"))
COURIER = {"host": "benchA", "addr": "member1", "state": "live", "can": ["flash"]}


# --- what a plugin may declare ---------------------------------------------------------------

def test_a_plugin_brings_a_delivering_route_as_data(widgets):
    assert widgets.route_delivery("courier") == {
        "bearer": "courier", "tool": "courier_route.py", "formats": ["widget-image"],
        "settings": "bench_courier", "credential": "bench_courier_key"}
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
    # it is read by the route it would otherwise be written by
    assert v["bench_routes"]["read"]["bearer"] == "usb"
    assert v["bench_delivery"] == {**widgets.route_delivery("courier"), "address": "member1"}


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


def test_a_core_flasher_that_hands_its_image_over_makes_its_families_deliverable(widgets):
    """An image written over MCUmgr is a signed MCUboot image, whatever family runs it: a route
    that carries one can take the flasher's place for any of them."""
    assert widgets.takes_delivery("zephyr_mcumgr", "smp", ["raucb", "mcuboot-bin"]) == ["mcuboot-bin"]
    assert widgets.takes_delivery("beacon", "smp", ["mcuboot-bin"]) == ["mcuboot-bin"]
    assert widgets.takes_delivery("beacon", "esptool", ["mcuboot-bin"]) == []
    assert widgets.takes_delivery("widget", None, ["widget-image", "mcuboot-bin"]) == ["widget-image"]


# --- the route a device is read by ---------------------------------------------------------------

ENTRY = {"reach": {"usb": {"host": "bench1", "can": ["shell", "diag"]},
                   "swd": {"host": "bench1", "state": "live", "can": ["flash", "diag"]},
                   "eth": {"host": "bench1", "ip": "192.0.2.20", "state": "live", "can": ["flash"]},
                   "courier": {"host": "bench1", "addr": "dev1", "state": "live", "can": ["flash"]}}}


def test_a_device_is_read_by_the_route_it_would_otherwise_be_written_by():
    chosen = routes.select(ENTRY, {"flash": "courier"}, delivering=("courier",))
    assert (chosen["flash"]["bearer"], chosen["read"]["bearer"]) == ("courier", "swd")
    chosen = routes.select(ENTRY, {"flash": "courier", "read": "eth"}, delivering=("courier",))
    assert (chosen["read"]["bearer"], chosen["read"]["ip"]) == ("eth", "192.0.2.20")
    assert "read" not in routes.select(ENTRY, {"flash": "eth"}, delivering=("courier",))


def test_a_read_pin_is_refused_where_it_means_nothing():
    with pytest.raises(routes.RouteError, match="not one that delivers"):
        routes.select(ENTRY, {"flash": "eth", "read": "swd"}, delivering=("courier",))
    with pytest.raises(routes.RouteError, match="delivers and reads nothing"):
        routes.select(ENTRY, {"flash": "courier", "read": "courier"}, delivering=("courier",))
    only = {"reach": {"usb": {"host": "bench1", "can": ["shell"]},
                      "courier": {"host": "bench1", "addr": "dev1", "state": "live", "can": ["flash"]}}}
    with pytest.raises(routes.RouteError, match="nothing to read it by"):
        routes.select(only, {"flash": "courier"}, delivering=("courier",))


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


# --- a device's credential on the route ----------------------------------------------------------

#: A stub route that keeps what it knows in a file beside it: {target: credential}.
BOOK = """
import os
book = os.path.join(os.path.dirname(__file__), "book.json")
known = json.load(open(book)) if os.path.exists(book) else {}
verb, t, c = sys.argv[1], req["target"], req.get("credential")
if verb == "enroll":
    out = {"created": t not in known, "changed": known.get(t) != c}
    known[t] = c
    json.dump(known, open(book, "w"))
elif verb == "state":
    out = {"known": t in known, "open": False, "result": None, "image": None, "detail": ""}
    if c:
        out["enrolled"] = known.get(t) == c
print(json.dumps(out))
"""


def test_the_credential_goes_to_the_tool_on_stdin_with_every_request(tmp_path):
    spec = {**stub_tool(tmp_path, "print(json.dumps({'argv': sys.argv[1:], 'saw': req}))\n"),
            "credential": "made-up-key"}
    out = delivery.Delivery(spec, "dev1").state()
    assert out["argv"] == ["state"] and out["saw"]["credential"] == "made-up-key"


def test_a_device_with_a_declared_credential_is_enrolled_and_then_left_alone(tmp_path):
    route = delivery.Delivery({**stub_tool(tmp_path, BOOK), "credential": "first"}, "dev1")
    assert "does not know 'dev1': to be enrolled" in route.enrollment()
    assert route.enroll() == {"created": True, "changed": True}
    assert route.enrollment() is None
    # the layout's credential changes: the route holds another until it is set
    route = delivery.Delivery({**stub_tool(tmp_path, BOOK), "credential": "second"}, "dev1")
    assert "with another credential: to be set" in route.enrollment()
    assert route.enroll() == {"created": False, "changed": True}
    assert route.enrollment() is None


def test_nothing_is_enrolled_without_a_declared_credential(tmp_path):
    route = delivery.Delivery(stub_tool(tmp_path, BOOK), "dev1")
    assert route.enrollment() is None
    with pytest.raises(common.FlashError, match="declares no credential"):
        route.enroll()


def test_a_route_that_does_not_know_the_device_blocks_a_delivery_unless_it_is_to_be_enrolled(tmp_path):
    plain = delivery.Delivery(stub_tool(tmp_path, BOOK), "dev1")
    assert "does not know the target 'dev1'" in plain.blockers({"format": "raucb"})[0]
    declared = delivery.Delivery({**stub_tool(tmp_path, BOOK), "credential": "k"}, "dev1")
    assert declared.blockers({"format": "raucb"}) == []
    # an image the route does not carry is refused before the route is asked anything
    assert "carries raucb" in declared.blockers({"format": "uf2"})[0]


def test_the_enroll_module_plans_then_enrolls_then_changes_nothing(tmp_path, monkeypatch):
    from ansible_collections.rxrbench.core.plugins.modules import route_enroll
    monkeypatch.setattr(route_enroll.common, "route_refusal", lambda *a: None)
    spec = {**stub_tool(tmp_path, BOOK), "credential": "made-up-0001", "address": "dev1"}

    def run(check, allowed=True, **over):
        out = {}

        class Module:
            check_mode = check
            params = {"board": "dev1", "devreg": "devreg.py", "apply_allowed": allowed,
                      "delivery": {**spec, **over}}

            def __init__(self, **_kw):
                pass

            def fail_json(self, **kw):
                out.update(failed=True, **kw)
                raise SystemExit(1)

            def exit_json(self, **kw):
                out.update(kw)
                raise SystemExit(0)

        monkeypatch.setattr(route_enroll, "AnsibleModule", Module)
        with pytest.raises(SystemExit):
            route_enroll.main()
        return out

    plan = run(check=True)
    assert plan["changed"] and "to be enrolled" in plan["step"]
    assert not os.path.exists(tmp_path / "book.json")           # a plan tells the route nothing
    assert "bench_apply_allowed" in run(check=False, allowed=False)["msg"]
    assert run(check=False)["changed"] is True
    assert run(check=False) == {"changed": False, "step": ""}
    assert "made-up-0001" not in plan["step"]
    changed = run(check=True, credential="made-up-0002")
    assert changed["changed"] and "another credential" in changed["step"]
    assert "made-up" not in changed["step"]


def test_the_enroll_module_refuses_an_address_the_registry_does_not_record(tmp_path, monkeypatch):
    from ansible_collections.rxrbench.core.plugins.modules import route_enroll
    monkeypatch.setattr(route_enroll.common, "route_refusal",
                        lambda devreg, name, bearer, addr: f"devreg guard refuses {addr} for {name} over {bearer}")
    out = {}

    class Module:
        check_mode = False
        params = {"board": "dev1", "devreg": "devreg.py", "apply_allowed": True,
                  "delivery": {**stub_tool(tmp_path, BOOK), "credential": "k", "address": "elsewhere"}}

        def __init__(self, **_kw):
            pass

        def fail_json(self, **kw):
            out.update(kw)
            raise SystemExit(1)

    monkeypatch.setattr(route_enroll, "AnsibleModule", Module)
    with pytest.raises(SystemExit):
        route_enroll.main()
    assert "devreg guard refuses elsewhere" in out["msg"] and not os.path.exists(tmp_path / "book.json")


def test_a_failed_runs_message_carries_what_the_route_recorded(tmp_path):
    def says(answer):
        return delivery.Delivery(stub_tool(tmp_path, f"print(json.dumps({answer!r}))\n"), "dev1").verdict()
    assert "says success: the record is wrong (action 7 finished)" in says(
        {"known": True, "open": False, "result": "success", "detail": "action 7 finished"})
    assert "says failure too" in says({"known": True, "open": False, "result": "failure", "detail": "gate failed"})
    assert "has not heard the outcome yet" in says({"known": True, "open": True, "result": None, "detail": ""})
    assert "could not be asked" in delivery.Delivery(
        stub_tool(tmp_path, "print(json.dumps({'error': 'down'}))\nsys.exit(1)\n"), "dev1").verdict()
