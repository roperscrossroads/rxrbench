"""devreg, the safety gate in front of every hardware write: it never guesses."""
import os
import subprocess
import sys

DEVREG = os.path.join(os.path.dirname(__file__), "..", "..", "devreg", "devreg.py")
REGISTRY = """version: 1
hosts: {}
devices:
  kitx:
    kind: serial
    destructive: true
    match:
      usb_serial: "0000000000000000"
protected:
  {host}: [/]
"""


def devreg(tmp_path, *args):
    reg = tmp_path / "registry.yaml"
    reg.write_text(REGISTRY.replace("{host}", os.uname().nodename))
    return subprocess.run([sys.executable, DEVREG, *args], capture_output=True, text=True,
                          env={**os.environ, "DEVREG_REGISTRY": str(reg)})




def test_resolve_fails_for_an_unknown_and_for_an_absent_device(tmp_path):
    assert devreg(tmp_path, "resolve", "nosuch").returncode != 0
    r = devreg(tmp_path, "resolve", "kitx")
    assert r.returncode == 1


def test_guard_refuses_a_path_that_is_not_the_named_device(tmp_path):
    assert devreg(tmp_path, "guard", "kitx", "/dev/null").returncode != 0


def _load():
    import importlib.util
    spec = importlib.util.spec_from_file_location("devreg_mod", DEVREG)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.dirname(DEVREG))
    spec.loader.exec_module(mod)
    return mod


def test_a_serialless_bridge_counts_only_beside_its_named_sibling():
    mod = _load()
    hub = "/sys/devices/usb1/1-1/1-1.4"
    ch340 = {"usb_vid": "1a86", "usb_pid": "7523", "syspath": hub + "/1-1.4.1"}
    rp2040 = {"usb_vid": "2886", "usb_pid": "0050", "serial": "E66038B7135D4F2A",
              "syspath": hub + "/1-1.4.2"}
    stray = {"usb_vid": "1a86", "usb_pid": "7523", "syspath": "/sys/devices/usb1/1-1/1-1.2"}

    class Fake(mod.Prober):
        def __init__(self, usb):
            super().__init__("local")
            self._d = {"usb": usb, "serial": [], "block": []}

    spec = {"kind": "usb", "match": {"usb_vid": "1a86", "usb_pid": "7523",
                                     "sibling_vid": "2886", "sibling_serial": "e66038b7135d4f2a"}}
    assert [h["syspath"] for h in mod.match_device(spec, Fake([ch340, rp2040, stray]))] == \
        [ch340["syspath"]]
    assert mod.match_device(spec, Fake([ch340, stray])) == [], "no sibling, no match"
    other = {**rp2040, "serial": "0000000000000000"}
    assert mod.match_device(spec, Fake([ch340, other])) == [], "the sibling's serial must match"


def test_a_bridge_another_entry_names_by_its_sibling_is_not_a_bare_vid_pid_entrys():
    """cyd1 registered by its CH340's VID:PID, board3 as the CH340 beside its own RP2040: with both
    plugged in cyd1 is not ambiguous, and with cyd1 unplugged it does not resolve to board3's
    bridge. board3's bridge is board3's, and only that."""
    mod = _load()
    hub = "/sys/devices/usb1/1-1/1-1.4"
    board3 = {"usb_vid": "1a86", "usb_pid": "7523", "syspath": hub + "/1-1.4.1", "busid": "1-1.4.1"}
    rp2040 = {"usb_vid": "2886", "usb_pid": "0050", "serial": "E66038B7135D4F2A",
              "syspath": hub + "/1-1.4.2", "busid": "1-1.4.2"}
    cyd = {"usb_vid": "1a86", "usb_pid": "7523", "syspath": "/sys/devices/usb1/1-1/1-1.3",
           "busid": "1-1.3"}

    class Fake(mod.Prober):
        def __init__(self, usb):
            super().__init__("local")
            self._d = {"usb": usb, "serial": [], "block": []}

    bare = {"kind": "usb", "match": {"usb_vid": "1a86", "usb_pid": "7523"}}
    reg = {"devices": {
        "cyd1": bare,
        "board3": {"kind": "usb", "match": {"usb_vid": "1a86", "usb_pid": "7523", "sibling_vid": "2886",
                                          "sibling_serial": "e66038b7135d4f2a"}},
        "board3-ui": {"kind": "usb", "match": {"usb_serial": "E66038B7135D4F2A"}}}}
    both = Fake([board3, rp2040, cyd])
    rc, hit, _ = mod.resolve_one(reg, "cyd1", both)
    assert (rc, hit["busid"]) == (mod.OK, "1-1.3")
    rc, hit, _ = mod.resolve_one(reg, "board3", both)
    assert (rc, hit["busid"]) == (mod.OK, "1-1.4.1")
    rc, _, _ = mod.resolve_one(reg, "cyd1", Fake([board3, rp2040]))
    assert rc == mod.NOTFOUND, "cyd1 unplugged: board3's bridge is not taken for it"
    # without the sibling the bridge is no one's but the bare entry's
    rc, hit, _ = mod.resolve_one(reg, "cyd1", Fake([board3]))
    assert (rc, hit["busid"]) == (mod.OK, "1-1.4.1")
    # two entries both by make alone claim nothing of each other: still refused
    two = {"devices": {"cyd1": bare, "cyd2": bare}}
    assert mod.resolve_one(two, "cyd1", Fake([cyd, board3]))[0] == mod.AMBIGUOUS


def test_a_board_with_two_usb_faces_matches_on_either():
    """A stock ESP32 app on native USB and the same chip's ROM / USB-Serial-JTAG face: `match`
    is the lasting face, `match_also` the other; a hit on either is the board, never two."""
    mod = _load()
    jtag = {"id": "usb-Espressif_USB_JTAG_serial_debug_unit_AA:BB:CC:DD:EE:01-if00", "path": "/dev/ttyACM3"}
    stock = {"id": "usb-Espressif_Systems_Some_Board_AABBCCDDEE01-if00", "path": "/dev/ttyACM4"}

    class Fake(mod.Prober):
        def __init__(self, serial):
            super().__init__("local")
            self._d = {"usb": [], "serial": serial, "block": []}

    spec = {"kind": "serial", "match": {"serial_by_id": jtag["id"]},
            "match_also": [{"serial_by_id": stock["id"]}]}
    assert [h["identifier"] for h in mod.match_device(spec, Fake([stock]))] == ["/dev/ttyACM4"]
    assert [h["identifier"] for h in mod.match_device(spec, Fake([jtag]))] == ["/dev/ttyACM3"]
    assert mod.match_device(spec, Fake([])) == []
    # both faces at once would be two devices: refused as ambiguous by resolve, not merged
    assert len(mod.match_device(spec, Fake([stock, jtag]))) == 2


# ---- routes that are not a path: the registry's address, looked up and never probed ----

ROUTES = """version: 1
hosts: {}
devices:
  kitx:
    kind: serial
    aliases: [kx]
    match: {usb_serial: "0000000000000000"}
    reach:
      usb: {host: bench1}
      ble: {host: bench1, addr: "02:00:00:00:00:AB", state: unproven}
      lora: {node: "!00000001"}
  pix:
    kind: linux
    match: {}
    reach:
      ssh: {host: bench1, ip: 192.0.2.7}
      wifi: {host: bench1, ip: 192.0.2.7, addr: 192.0.2.8}
  nf7:
    kind: swd
    reach:
      eth: {host: bench1, ip: 192.0.2.190, port: 1337, state: live, can: [flash]}
"""


def routes(tmp_path, *args):
    reg = tmp_path / "registry.yaml"
    reg.write_text(ROUTES)
    return subprocess.run([sys.executable, DEVREG, *args], capture_output=True, text=True,
                          env={**os.environ, "DEVREG_REGISTRY": str(reg)})


def test_resolve_prints_the_registrys_address_for_a_route(tmp_path):
    assert routes(tmp_path, "resolve", "--route", "ble", "kitx").stdout == "02:00:00:00:00:AB\n"
    assert routes(tmp_path, "resolve", "--route", "ble", "kx").stdout == "02:00:00:00:00:AB\n"
    assert routes(tmp_path, "resolve", "--route", "ssh", "pix").stdout == "192.0.2.7\n"


def test_a_route_the_registry_does_not_record_is_not_found_and_two_addresses_are_ambiguous(tmp_path):
    for args, rc, why in ((("ssh", "kitx"), 1, "no ssh route"),
                          (("lora", "kitx"), 1, "no address"),
                          (("wifi", "pix"), 2, "REFUSING to guess"),
                          (("ble", "nosuch"), 4, "unknown device")):
        r = routes(tmp_path, "resolve", "--route", *args)
        assert r.returncode == rc and why in r.stderr and r.stdout == "", args


def test_guard_passes_only_the_registrys_address_for_a_route(tmp_path):
    assert routes(tmp_path, "guard", "--route", "ble", "kitx", "02:00:00:00:00:ab").returncode == 0
    r = routes(tmp_path, "guard", "--route", "ble", "kitx", "02:00:00:00:00:AC")
    assert r.returncode == 3 and "02:00:00:00:00:AB" in r.stderr and "02:00:00:00:00:AC" in r.stderr
    assert routes(tmp_path, "guard", "--route", "ssh", "kitx", "192.0.2.7").returncode == 1
    assert routes(tmp_path, "guard", "--route", "ssh", "pix", "192.0.2.9").returncode == 3


def test_lint_names_the_keys_beyond_the_contract_and_refuses_wrong_shapes(tmp_path):
    reg = tmp_path / "registry.yaml"
    reg.write_text("""version: 1
hosts:
  bench1: {ssh: "bench@bench1", bench_dir: /home/bench/rxrbench, protected: [sda]}
protected: []
devices:
  a:
    kind: serial
    notes: prose the tool never reads
    match: {usb_serial: "0000000000000001"}
    reach:
      ble: {addr: "AA:BB:CC:DD:EE:01", state: live, can: [flash], confirmed: yes}
      lora: {node: "!abcd", state: dormant}
  b:
    kind: serial
    aliases: [a]
    match: {host: bench1}
    reach: {wifi: {ip: 192.0.2.9}}
""")
    env = {**os.environ, "DEVREG_REGISTRY": str(reg)}
    r = subprocess.run([sys.executable, DEVREG, "lint"], capture_output=True, text=True, env=env)
    out = r.stdout
    assert "hosts.*: bench_dir" in out and "devices.*: notes" in out, out
    assert "confirmed" not in [ln.split(":")[-1].strip().split()[0] for ln in out.splitlines() if ln.startswith("  ")], \
        "a display-only key the contract names is not unknown"
    assert "PROBLEM devices.b.match: names nothing that matches" in out
    assert "PROBLEM devices.b.reach.wifi: no state" in out
    assert "PROBLEM hosts.bench1.protected: 'sda' is not an absolute path" in out
    assert "alias 'a' names more than one device" in out
    assert r.returncode == 4, "a wrong shape is refused"
    clean = tmp_path / "clean.yaml"
    clean.write_text("""version: 1
hosts: {bench1: {ssh: "bench@bench1"}}
protected: []
devices:
  a: {kind: serial, owner: me, match: {usb_serial: "0000000000000001"}, reach: {usb: {host: bench1}}}
""")
    env["DEVREG_REGISTRY"] = str(clean)
    assert subprocess.run([sys.executable, DEVREG, "lint"], capture_output=True, text=True, env=env).returncode == 0
    r = subprocess.run([sys.executable, DEVREG, "lint", "--strict"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, "owner is written by enroll: part of the contract's shape"
    noisy = tmp_path / "noisy.yaml"
    noisy.write_text("version: 1\nhosts: {}\nprotected: []\ndevices:\n  a: {kind: serial, custody: here, match: {usb_vid: '1a86'}}\n")
    env["DEVREG_REGISTRY"] = str(noisy)
    assert subprocess.run([sys.executable, DEVREG, "lint", "--strict"], capture_output=True, text=True, env=env).returncode == 4


def test_resolve_busid_prints_where_on_the_bus_the_device_is():
    """A debug probe is handed to openocd by its USB location: two probes of one kind on a host
    each reach their own chip (a clone's serial is often unreadable, so not by serial)."""
    mod = _load()

    class Fake(mod.Prober):
        def __init__(self):
            super().__init__("local")
            self._d = {"usb": [{"usb_vid": "0483", "usb_pid": "374b", "serial": "0671FF48",
                                "busid": "1-1.2.4", "syspath": "/sys/devices/usb1/1-1/1-1.2.4"},
                               {"usb_vid": "0483", "usb_pid": "3748", "serial": "",
                                "busid": "1-1.3", "syspath": "/sys/devices/usb1/1-1/1-1.3"}],
                       "serial": [], "block": []}

    reg = {"devices": {"onboard": {"kind": "usb", "match": {"usb_vid": "0483", "usb_pid": "374b",
                                                            "usb_serial": "0671FF48"}},
                       "clone": {"kind": "usb", "match": {"usb_vid": "0483", "usb_pid": "3748"}}}}
    for name, want in (("onboard", "1-1.2.4"), ("clone", "1-1.3")):
        rc, hit, _ = mod.resolve_one(reg, name, Fake())
        assert rc == mod.OK and mod.busid_of(hit) == want
    assert mod.busid_of({"identifier": "/dev/ttyACM0"}) is None


def test_a_wired_network_route_resolves_and_guards_like_any_address(tmp_path):
    assert routes(tmp_path, "resolve", "--route", "eth", "nf7").stdout == "192.0.2.190\n"
    assert routes(tmp_path, "guard", "--route", "eth", "nf7", "192.0.2.190").returncode == 0
    assert routes(tmp_path, "guard", "--route", "eth", "nf7", "192.0.2.191").returncode != 0
    lint = routes(tmp_path, "lint")
    assert "devices.*.reach: eth" not in lint.stdout + lint.stderr, "eth is a bearer the contract knows"
