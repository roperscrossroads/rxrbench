import importlib.util
import os

spec = importlib.util.spec_from_file_location(
    "usbipd_attach", os.path.join(os.path.dirname(__file__), "..", "..", "tools", "usbipd_attach.py"))
ua = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ua)

LISTING = """Connected:
BUSID  VID:PID    DEVICE                                                        STATE
1-4    3554:fc03  USB Input Device                                              Not shared
6-1    1a86:7523  USB-SERIAL CH340 (COM29)                                      Attached
6-2    2886:0050  USB Serial Device (COM30)                                     Shared
6-3    2886:0050  USB Serial Device (COM31)                                     Not shared

Persisted:
GUID                                  DEVICE
00111211-29fb-4449-81ea-313a83f8510f  USB Serial Device (COM26)
"""


def test_the_connected_rows_are_read_and_the_persisted_ones_are_not():
    rows = ua.parse_list(LISTING)
    assert [r[0] for r in rows] == ["1-4", "6-1", "6-2", "6-3"]
    assert rows[1] == ("6-1", "1a86:7523", "USB-SERIAL CH340 (COM29)", "Attached")


def test_the_plan_attaches_shared_ones_and_explains_the_rest():
    devices = {
        "board-a": {"match": {"usb_vid": "1a86", "usb_pid": "7523"}},
        "board-b": {"reach": {"usb": {"modes": {"app": {"usb": "2886:0050"}}}}},
        "board-c": {"match": {"usb_vid": "dead", "usb_pid": "beef"}},
        "board-d": {"match": {"serial_by_id": "x"}},
    }
    p = ua.plan(devices, ua.parse_list(LISTING), ["board-a", "board-b", "board-c", "board-d", "nope"])
    got = {(n, a) for n, a, _d in p}
    assert ("board-a", "attached") in got
    assert ("board-b", "attach") in got and ("board-b", "not-shared") in got
    assert ("board-c", "absent") in got
    assert ("board-d", "no-vidpid") in got
    assert ("nope", "unknown") in got


def _run(tmp_path, monkeypatch, capsys, argv, hosts):
    reg = tmp_path / "registry.yaml"
    reg.write_text("hosts: %s\ndevices:\n  near: {reach: {usb: {host: here}}}\n"
                   "  far: {reach: {usb: {host: there}}}\n" % hosts)
    monkeypatch.setenv("DEVREG_REGISTRY", str(reg))
    monkeypatch.setattr(ua, "USBIPD", "/nonexistent/usbipd.exe")   # stops before attaching
    rc = ua.main(argv)
    return rc, capsys.readouterr().err


def test_the_workstation_is_the_host_devreg_takes_for_this_machine(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "box")
    reg = {"hosts": {"there": {"ssh": "u@there"}, "here": {"local": True}}}
    assert ua.workstation(reg) == "here"


def test_the_workstation_matches_by_hostname_before_the_local_flag(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "box.example")
    reg = {"hosts": {"here": {"local": True}, "there": {"hostname": "box"}}}
    assert ua.workstation(reg) == "there"


def test_no_workstation_and_no_names_is_refused(tmp_path, monkeypatch, capsys):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "box")
    rc, err = _run(tmp_path, monkeypatch, capsys, [], "{there: {ssh: u@there}}")
    assert rc == 2 and "--host" in err and "local: true" in err


def test_an_explicit_host_needs_no_registry_entry_for_this_machine(tmp_path, monkeypatch, capsys):
    rc, err = _run(tmp_path, monkeypatch, capsys, ["--host", "here"], "{}")
    assert rc == 2 and "cannot run" in err          # got as far as usbipd: the host was accepted
