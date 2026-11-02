"""devlock: a named resource (a host's one Bluetooth adapter) locks like a device path, and
`run` holds a lock around a command for tools that cannot take it in-process."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "devreg"))
import devlock  # noqa: E402  pylint: disable=wrong-import-position

DEVLOCK = os.path.join(os.path.dirname(__file__), "..", "..", "devreg", "devlock.py")


def _env(tmp_path):
    return {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)}


def test_a_named_resource_is_keyed_verbatim_and_a_device_path_is_not(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert devlock.is_resource("ble:hci0")
    assert not devlock.is_resource("/dev/ttyACM0")
    assert not devlock.is_resource("ble")              # no name
    assert devlock.lock_key("ble:hci0") == "ble_hci0"
    assert devlock.lock_path("ble:hci0").parent == tmp_path / "devlock"
    assert devlock.open_by("ble:hci0") == []           # nothing to find in /proc


def test_two_holders_of_the_adapter_exclude_each_other(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with devlock.DevLock("ble:hci0", tool="first") as a:
        assert a.held
        b = devlock.DevLock("ble:hci0", tool="second")
        assert not b.acquire(wait_s=0.3)
        assert "first" in b.holder_summary()
        b.release()
    assert devlock.probe("ble:hci0") is None


def test_run_holds_the_lock_for_the_command_and_returns_its_status(tmp_path):
    env = _env(tmp_path)
    # The child sees the lock held (by its parent `run`), and its exit status comes back.
    probe = (f"import sys; sys.path.insert(0, {os.path.dirname(DEVLOCK)!r}); import devlock; "
             f"m = devlock.probe('ble:hci0'); sys.exit(0 if m and m['tool'] == 'rx' else 9)")
    r = subprocess.run([sys.executable, DEVLOCK, "run", "ble:hci0", "--tool", "rx", "--",
                        sys.executable, "-c", probe], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, DEVLOCK, "run", "ble:hci0", "--tool", "rx", "--",
                        sys.executable, "-c", "import sys; sys.exit(3)"], env=env)
    assert r.returncode == 3
    assert subprocess.run([sys.executable, DEVLOCK, "who", "ble:hci0"], env=env,
                          capture_output=True, text=True).stdout.strip() == "free"


def test_run_reports_a_busy_resource_and_does_not_run_the_command(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    marker = tmp_path / "ran"
    with devlock.DevLock("ble:hci0", tool="holder"):
        r = subprocess.run([sys.executable, DEVLOCK, "run", "ble:hci0", "--tool", "rx",
                            "--wait", "0.2", "--", "touch", str(marker)],
                           env=_env(tmp_path), capture_output=True, text=True)
    assert r.returncode == 75
    assert "holder" in r.stderr
    assert not marker.exists()


def test_every_tool_on_a_host_finds_one_lock_directory(tmp_path, monkeypatch):
    # Named outright:
    monkeypatch.setenv("DEVLOCK_DIR", str(tmp_path / "named"))
    assert devlock.lock_path("ble:hci0").parent == tmp_path / "named"
    # The runtime dir from the environment:
    monkeypatch.delenv("DEVLOCK_DIR")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert devlock.lock_path("ble:hci0").parent == tmp_path / "devlock"
    # A process started without it (cron, a bare ssh command) finds the same runtime dir where
    # logind keeps it, not a directory of its own under /tmp.
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    run = f"/run/user/{os.getuid()}"
    want = (run if os.path.isdir(run) else f"/tmp/devlock-{os.getuid()}") + "/devlock"
    assert str(devlock.lock_path("ble:hci0").parent) == want


def _fake_tty(root, name, usb_dir, serial=None):
    """/dev/<name> on a USB device at sysfs @usb_dir (with a serial file when given)."""
    dev = root / "dev" / name
    dev.parent.mkdir(parents=True, exist_ok=True)
    dev.touch()
    usb = root / "sys" / "devices" / usb_dir
    intf = usb / f"{usb_dir}:1.0"
    intf.mkdir(parents=True, exist_ok=True)
    (usb / "idVendor").write_text("1a86\n")
    if serial:
        (usb / "serial").write_text(serial + "\n")
    tty = root / "sys" / "class" / "tty" / name
    tty.mkdir(parents=True, exist_ok=True)
    (tty / "device").symlink_to(intf)
    return dev


def test_two_serial_less_bridges_are_locked_by_their_port_not_by_their_shared_by_id_name(tmp_path):
    # Two CH340 boards: one by-id name for both, pointing at the one
    # plugged in last. Keyed by it, a broker holding the first board's port held the second's.
    a = _fake_tty(tmp_path, "ttyUSB0", "1-1.3")
    b = _fake_tty(tmp_path, "ttyUSB1", "1-1.2.4")
    by_id, by_path = tmp_path / "by-id", tmp_path / "by-path"
    by_id.mkdir()
    by_path.mkdir()
    (by_id / "usb-1a86_USB_Serial-if00-port0").symlink_to(b)
    (by_path / "platform-usb-0:1.3:1.0-port0").symlink_to(a)
    (by_path / "platform-usbv2-0:1.3:1.0-port0").symlink_to(a)
    (by_path / "platform-usb-0:1.2.4:1.0-port0").symlink_to(b)
    kw = {"by_id": str(by_id), "by_path": str(by_path), "sys_tty": str(tmp_path / "sys/class/tty")}
    ka, kb = devlock.lock_key(str(a), **kw), devlock.lock_key(str(b), **kw)
    assert ka == "platform-usb-0_1.3_1.0-port0"
    assert kb == "platform-usb-0_1.2.4_1.0-port0"


def test_a_bridge_with_a_serial_keeps_its_by_id_name(tmp_path):
    a = _fake_tty(tmp_path, "ttyACM0", "1-1.2", serial="0123456789ABCDEF")
    by_id, by_path = tmp_path / "by-id", tmp_path / "by-path"
    by_id.mkdir()
    by_path.mkdir()
    (by_id / "usb-ZEPHYR_0123456789ABCDEF-if00").symlink_to(a)
    (by_path / "platform-usb-0:1.2:1.0").symlink_to(a)
    kw = {"by_id": str(by_id), "by_path": str(by_path), "sys_tty": str(tmp_path / "sys/class/tty")}
    assert devlock.lock_key(str(a), **kw) == "usb-ZEPHYR_0123456789ABCDEF-if00"
