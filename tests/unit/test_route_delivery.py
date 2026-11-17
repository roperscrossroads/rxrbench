"""A route that delivers an image itself: what a plugin declares, what a layout is told or
refused, and the RAUC flasher handing its write to such a route while it still reads the
device before and after."""
import os
import threading
import time

import pytest
import yaml

from ansible_collections.rxrbench.core.plugins.module_utils import layout
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, delivery
from ansible_collections.rxrbench.core.plugins.plugin_utils import families
from ansible_collections.rxrbench.hawkbit.sim import fakeserver
from ansible_collections.rxrbench.linux.plugins.module_utils import rauc

from .test_flash_rauc import NEW, OLD, FakeTarget

pytestmark = pytest.mark.plugins

RX = os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections", "rxrbench")
TOOL = os.path.abspath(os.path.join(RX, "hawkbit", "host", "bin", "hawkbit_route.py"))
MANIFEST = {"family": "linux_rauc", "format": "raucb", "target": "example-pi", "file": "u.raucb", **NEW}


# --- what a plugin declares ------------------------------------------------------------------

def test_the_hawkbit_plugin_brings_a_route_and_no_family():
    assert families.owner("linux_rauc") == "rxrbench.linux"
    assert "rxrbench.hawkbit" not in {families.owner(f) for f in families.FAMILIES if families.owner(f)}
    how = families.route_delivery("hawkbit")
    assert (how["bearer"], how["tool"], how["settings"]) == ("hawkbit", "hawkbit_route.py", "bench_hawkbit")
    assert "raucb" in how["formats"]
    assert families.route_delivery("ssh") is None and families.route_delivery("usb") is None


def test_only_a_family_that_says_so_takes_a_delivered_image():
    assert families.plugin("linux_rauc").takes_delivery == ("raucb",)
    assert families.plugin("linux_slotguard").takes_delivery == ("raucb",)
    assert families.plugin("linux").takes_delivery == ()
    assert families.plugin("zephyr_mcumgr").takes_delivery == ()
    # ... and a Zephyr image written over MCUmgr is handed over by its flasher, not its family
    assert families.takes_delivery("zephyr_mcumgr", "smp", families.route_delivery("hawkbit")["formats"]) == [
        "mcuboot-bin"]
    assert families.takes_delivery("linux", "rauc", families.route_delivery("hawkbit")["formats"]) == []


# --- what a layout is told, or refused ---------------------------------------------------------

SIM = os.path.join(RX, "linux", "sim", "vm")


def resolved(tmp_path, family, routes=None, reach=None):
    """The simulated A/B device's layout, with a `hawkbit` route in its registry entry."""
    reg = layout.load_registry(os.path.join(SIM, "registry.yaml"))
    reg["devices"]["ab1"].setdefault("reach", {}).update(
        reach if reach is not None else {"hawkbit": {"host": "simhost", "addr": "ab1", "state": "live",
                                                     "can": ["flash"]}})
    hv = tmp_path / "host_vars"
    hv.mkdir(exist_ok=True)
    doc = {"firmware": {"family": family, "image": "sim-ab-plain"},
           "bench_rauc_remote_bundle": "/data/update.raucb"}
    if routes:
        doc["routes"] = routes
    (hv / "ab1.yml").write_text(yaml.safe_dump(doc))
    return layout.load_layout(str(tmp_path), {"boards": {"ab1": {"host": "simhost"}},
                                              "catalog_dir": os.path.join(SIM, "catalog")}, reg)


def test_a_pinned_delivering_route_reaches_the_update_step_as_data(tmp_path):
    v = resolved(tmp_path, "linux_rauc", {"flash": "hawkbit"})["boards"]["ab1"]["vars"]
    assert v["bench_routes"]["flash"]["bearer"] == "hawkbit"
    assert v["bench_routes"]["flash"]["addr"] == "ab1"
    assert v["bench_routes"]["read"]["bearer"] == "ssh"           # it is still read over ssh
    assert v["bench_delivery"] == {"bearer": "hawkbit", "tool": "hawkbit_route.py", "address": "ab1",
                                   "formats": ["raucb", "mcuboot-bin"], "settings": "bench_hawkbit",
                                   "credential": "bench_hawkbit_token"}


def test_without_the_pin_the_device_is_written_as_before(tmp_path):
    v = resolved(tmp_path, "linux_rauc")["boards"]["ab1"]["vars"]
    assert v["bench_routes"]["flash"]["bearer"] == "ssh" and "bench_delivery" not in v


def test_a_family_that_cannot_take_a_delivery_is_refused_with_the_reason(tmp_path):
    with pytest.raises(layout.LayoutError, match="delivers an image itself.*cannot take a delivered image"):
        resolved(tmp_path, "linux", {"flash": "hawkbit"})


def test_a_pin_the_registry_does_not_list_or_that_is_not_live_is_refused(tmp_path):
    with pytest.raises(layout.LayoutError, match="pinned to hawkbit, which the registry does not list"):
        resolved(tmp_path, "linux_rauc", {"flash": "hawkbit"}, reach={})
    with pytest.raises(layout.LayoutError, match="pinned to hawkbit, which is unproven"):
        resolved(tmp_path, "linux_rauc", {"flash": "hawkbit"},
                 reach={"hawkbit": {"host": "simhost", "addr": "ab1", "can": ["flash"]}})


# --- the RAUC flasher, its write handed to the route -----------------------------------------------

class Device(FakeTarget):
    """test_flash_rauc's scripted device, with a kernel boot id that changes at a reboot."""
    boots = 1

    def run(self, command, timeout=30):
        if "boot_id" in command:
            return 0, f"00000000-0000-4000-8000-{self.boots:012d}\n"
        if "reboot" in command:
            self.boots += 1
        return super().run(command, timeout)


class Bench:
    """A stand-in server, a scripted device (test_flash_rauc's) that is a target on it, and
    the device's own update client as a thread: it asks the server, "installs", reboots, and
    reports afterwards what it found, honestly or not."""

    def __init__(self, tmp_path, server, device, client="honest"):
        self.server, self.device, self.client = server, device, client
        self.bundle = str(tmp_path / "u.raucb")
        with open(self.bundle, "wb") as f:
            f.write(b"a signed bundle")
        self.spec = {"bearer": "hawkbit", "tool": TOOL, "formats": ["raucb"],
                     "settings": {"url": server.url, "user": "bench", "password": "made-up"}}
        self.route = delivery.Delivery(self.spec, "pi-a")
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._client, daemon=True)

    def enrol(self):
        delivery.Delivery({**self.spec, "credential": "t"}, "pi-a").enroll()
        return self

    def flasher(self, **kw):
        return rauc.RaucFlasher(self.device, MANIFEST, self.bundle, "/data/update.raucb",
                                compatible="example-pi", reboot_timeout=10, delivery=self.route,
                                delivery_timeout=kw.pop("delivery_timeout", 10), sleep=0.05, **kw)

    def _client(self):
        dev = fakeserver.Device(self.server.url, "pi-a", "t")
        while not self.stop.is_set():
            action = dev.poll()
            if action is None:
                time.sleep(0.05)
                continue
            dev.fetch()
            if self.client == "fails-install":
                dev.feedback(action, "closed", "failure", "rauc install failed: wrong compatible")
                return
            self.device.run("rauc install /data/hawkbit/bundle.raucb")
            if self.client == "naive":
                dev.feedback(action, "closed", "success", "installed (reported before any reboot)")
            else:
                dev.feedback(action, "proceeding", "none", "installed, rebooting")
            self.device.run("reboot")
            if self.client == "naive":
                return
            while not self.device.up():
                time.sleep(0.02)
            ok = self.device.booted == "B"
            dev.feedback(action, "closed", "success" if ok else "failure",
                         "slot B marked good" if ok else "rolled back: slot A is running again")
            return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.thread.join(5)


@pytest.fixture
def server():
    with fakeserver.FakeHawkbit() as hb:
        yield hb


def settled(bench, flasher, r):
    """The route's record beside the device's answer, once the device's client has spoken."""
    bench.thread.join(5)
    return bench.route.record(flasher.delivered, r["image"], r["after"])


def test_a_delivered_bundle_is_fetched_by_the_device_and_read_back_over_ssh(tmp_path, server):
    device = Device()
    with Bench(tmp_path, server, device).enrol() as bench:
        flasher = bench.flasher()
        r = common.drive(flasher, MANIFEST)
        assert r["changed"] and r["after"]["hash"] == NEW["hash"]
        assert not device.copied                         # nothing was pushed from the bench host
        assert flasher.delivered["id"] and not flasher.delivered["already"]
        record = settled(bench, flasher, r)
    assert record["route_says"]["result"] == "success" and record["agrees"] is True
    assert (record["bearer"], record["address"]) == ("hawkbit", "pi-a")


def test_a_device_already_on_the_bundle_is_not_delivered_to(tmp_path, server):
    device = Device()
    device.booted, device.slots = "B", {"A": OLD, "B": NEW}
    with Bench(tmp_path, server, device).enrol() as bench:
        r = common.drive(bench.flasher(), MANIFEST)
    assert not r["changed"] and not server.actions and not server.modules


def test_a_plan_writes_nothing_to_the_server_or_the_device(tmp_path, server):
    device = Device()
    with Bench(tmp_path, server, device).enrol() as bench:
        del server.requests[:]
        r = common.drive(bench.flasher(), MANIFEST, check_mode=True)
    assert r["changed"]
    assert server.requests and {m for m, _ in server.requests} == {"GET"}
    assert not device.installed and not server.actions


def test_a_rollback_fails_the_apply_and_the_route_agrees(tmp_path, server):
    device = Device(rolls_back=True)
    with Bench(tmp_path, server, device).enrol() as bench:
        with pytest.raises(common.FlashError, match="rolled back"):
            common.drive(bench.flasher(), MANIFEST)
        bench.thread.join(5)
        assert bench.route.state()["result"] == "failure"


def test_a_rollback_fails_the_apply_even_when_the_server_was_told_success(tmp_path, server):
    """The device's client reported success right after the install (as a stock updater does):
    the server reads success, the device is on its old slot, and the apply still fails."""
    device = Device(rolls_back=True)
    with Bench(tmp_path, server, device, client="naive").enrol() as bench:
        with pytest.raises(common.FlashError, match="rolled back"):
            common.drive(bench.flasher(), MANIFEST)
        bench.thread.join(5)
        assert bench.route.state()["result"] == "success"        # the record that is wrong
        record = bench.route.record(None, MANIFEST, {"hash": OLD["hash"]})
    assert record["agrees"] is False


def test_an_install_that_fails_on_the_device_ends_the_wait_with_the_devices_words(tmp_path, server):
    device = Device()
    with Bench(tmp_path, server, device, client="fails-install").enrol() as bench:
        with pytest.raises(common.FlashError, match="failed before the device rebooted.*wrong compatible"):
            common.drive(bench.flasher(), MANIFEST)
    assert device.booted == "A" and not device.installed


def test_a_device_that_never_asks_times_the_delivery_out(tmp_path, server):
    device = Device()
    bench = Bench(tmp_path, server, device).enrol()          # its client is never started
    with pytest.raises(common.FlashError, match="did not take the hawkbit delivery and reboot"):
        common.drive(bench.flasher(delivery_timeout=0.3), MANIFEST)


def test_a_target_the_server_does_not_know_is_refused_before_anything_is_delivered(tmp_path, server):
    bench = Bench(tmp_path, server, Device())            # not enrolled
    with pytest.raises(common.FlashError, match="does not know the target 'pi-a'"):
        common.drive(bench.flasher(), MANIFEST)
    assert not server.modules


def test_a_delivery_still_in_flight_is_not_started_again(tmp_path, server):
    device = Device()
    bench = Bench(tmp_path, server, device).enrol()
    bench.route.deliver(bench.bundle, MANIFEST)                # assigned, the device has not asked
    with pytest.raises(common.FlashError, match="still has a delivery in flight"):
        common.drive(bench.flasher(), MANIFEST)
    assert len(server.actions) == 1


def test_the_slot_must_be_good_before_a_delivery_as_before_a_push(tmp_path, server):
    bench = Bench(tmp_path, server, Device(boot_status="bad")).enrol()
    with pytest.raises(common.FlashError, match="not marked good"):
        common.drive(bench.flasher(), MANIFEST)
    assert not server.actions


def test_a_device_with_a_declared_token_is_planned_before_the_server_knows_it(tmp_path, server):
    """The first plan of a device the server has never heard of: the run will enroll it, so
    the flasher does not refuse, and the plan still tells the server nothing."""
    bench = Bench(tmp_path, server, Device())
    bench.route = delivery.Delivery({**bench.spec, "credential": "t"}, "pi-a")
    r = common.drive(bench.flasher(), MANIFEST, check_mode=True)
    assert r["changed"] and not server.targets and not server.modules
    assert "to be enrolled" in bench.route.enrollment()


def test_the_plugins_simulated_registry_holds_two_simulators_devices_and_each_takes_its_own(monkeypatch):
    """hb1 is the Linux plugin's A/B machine and zh1 core's emulated MCU. The A/B machine's
    tool is given this registry whole, and must take from it only the device it can run."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("simvm_under_test", os.path.join(RX, "linux", "sim", "simvm.py"))
    simvm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(simvm)
    monkeypatch.setattr(simvm, "REGISTRY", os.path.join(RX, "hawkbit", "sim", "registry.yaml"))
    assert simvm.devices() == {"hb1": 22212}
    reg = layout.load_registry(os.path.join(RX, "hawkbit", "sim", "registry.yaml"))
    assert set(reg["devices"]) == {"hb1", "zh1"}
    assert all("hawkbit" in d["reach"] for d in reg["devices"].values())
