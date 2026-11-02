"""usb_survey in check mode: the passive survey only, never a probe that resets a board."""
import importlib
import os

import pytest

us = importlib.import_module("ansible_collections.rxrbench.core.plugins.modules.usb_survey")


class Mod:
    def __init__(self, check_mode, deep):
        self.check_mode, self.warnings, self.result = check_mode, [], None
        self.params = {"deep": deep}

    def warn(self, msg):
        self.warnings.append(msg)

    def exit_json(self, **kw):
        self.result = kw
        raise SystemExit(0)


@pytest.mark.parametrize("check, deep, ran_deep", [(False, True, True), (True, True, False),
                                                   (True, False, False), (False, False, False)])
def test_the_deep_probe_is_left_out_of_a_plan_and_the_plan_says_so(monkeypatch, check, deep, ran_deep):
    mod, seen = Mod(check, deep), {}
    monkeypatch.setattr(us, "AnsibleModule", lambda **_kw: mod)
    monkeypatch.setattr(us, "survey", lambda p: seen.update(p) or [])
    with pytest.raises(SystemExit):
        us.main()
    assert seen["deep"] is ran_deep and mod.result == {"changed": False, "devices": [], "held": []}
    assert bool(mod.warnings) is (check and deep)


def test_an_rp2040_rom_bootloader_is_told_by_the_flash_id_picotool_selects(tmp_path):
    """An erased RP2040 shows only its ROM face, whose USB serial is not the flash id it is
    registered by: picotool, asked about this USB device alone, finds it by one of the
    registered ids or by none."""
    from ansible_collections.rxrbench.core.plugins.modules import usb_survey
    dev = tmp_path / "1-1.3"
    dev.mkdir()
    (dev / "busnum").write_text("1\n")
    (dev / "devnum").write_text("49\n")
    usb = {"syspath": str(dev), "serial": "0A1B2C3D4E5F"}
    devices = {"kb9": {"match": {"usb_serial": "0A0B0C0D0E0F1011"}},
               "other": {"match": {"usb_serial": "1A1B1C1D1E1F2021"}},
               "esp": {"match": {"serial_by_id": "usb-x"}}}
    asked = []

    def run(argv):
        asked.append(argv)
        ok = argv[argv.index("--ser") + 1] == "0A0B0C0D0E0F1011"
        out = "Device Information\n type: RP2040\n" if ok else "No accessible RP-series devices ..."
        return type("R", (), {"stdout": out, "stderr": ""})()
    assert usb_survey.rp2040_rom_owner(usb, devices, run=run) == ("kb9", "0A0B0C0D0E0F1011")
    assert all(a[a.index("--bus") + 1] == "1" and a[a.index("--address") + 1] == "49" for a in asked), \
        "only this USB device is asked"
    assert usb_survey.rp2040_rom_owner(usb, {"other": devices["other"]}, run=run) == (None, None)


def test_a_usb_tty_without_a_by_id_name_still_has_its_port(tmp_path, monkeypatch):
    """Two CH340s want one by-id name: the one udev leaves without it is still found, by its tty."""
    byid, ttys = tmp_path / "serial" / "by-id", tmp_path / "tty"
    byid.mkdir(parents=True)
    os.symlink("/dev/ttyUSB1", byid / "usb-1a86_USB_Serial-if00-port0")
    for name in ("ttyUSB0", "ttyUSB1", "ttyS0"):
        (ttys / name / "device").mkdir(parents=True)
    owners = {"/dev/ttyUSB1": "/sys/x/1-1.4.1", "/dev/ttyUSB0": "/sys/x/1-1.3"}   # ttyS0: no USB
    monkeypatch.setattr(us, "owning_usb", lambda path: owners.get(path))
    got = us.by_id(str(byid), ttys=str(ttys))
    assert got == {"/sys/x/1-1.4.1": [{"id": "usb-1a86_USB_Serial-if00-port0", "path": "/dev/ttyUSB1"}],
                   "/sys/x/1-1.3": [{"id": "", "path": "/dev/ttyUSB0"}]}
    disks = tmp_path / "disk" / "by-id"
    disks.mkdir(parents=True)
    assert us.by_id(str(disks), ttys=str(ttys)) == {}, "ttys are a serial directory's fallback only"


def test_the_hosts_own_keyboard_and_mouse_are_not_offered_unless_registered(monkeypatch, tmp_path):
    """A keyboard or mouse (HID only, a boot keyboard or mouse interface, no serial port, no
    disk) is the bench host's own, never a device under test: the survey leaves it out, unless
    the registry names it (a keyboard whose firmware is being worked on)."""
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families
    survey_mod = us
    keyboard = {"syspath": "/sys/bus/usb/devices/1-1.5", "busid": "1-1.5", "usb_vid": "046d",
                "usb_pid": "c31c", "serial": "", "manufacturer": "Logitech", "product": "USB Keyboard",
                "interfaces": ["03/01/01", "03/00/00"]}
    reg = tmp_path / "registry.yaml"
    monkeypatch.setattr(survey_mod, "usb_devices", lambda: [keyboard])
    monkeypatch.setattr(survey_mod, "by_id", lambda d: {})
    args = {"registry": str(reg), "host": "h1", "only": None, "deep": False,
            "families": families.specs(), "boards": {}}
    reg.write_text("devices: {}\n")
    assert survey_mod.survey(args) == []
    reg.write_text("devices:\n  kbd1: {kind: usb, match: {usb_vid: '046d', usb_pid: 'c31c'}}\n")
    (rec,) = survey_mod.survey(args)
    assert rec["registered"] == ["kbd1"] and rec["class"]["kind"] == "input"


def test_the_watchers_answer_for_the_same_enumeration_names_the_rom(tmp_path):
    """The plug watcher already asked this bootloader (docs/ARCHITECTURE.md#plug-watcher-and-busy-marks): its answer is
    taken, for this enumeration only, and no second picotool ask is needed."""
    from ansible_collections.rxrbench.core.plugins.modules import usb_survey
    dev = tmp_path / "1-1.3"
    dev.mkdir()
    (dev / "devnum").write_text("49\n")
    usb = {"syspath": str(dev), "busid": "1-1.3", "serial": "A1A2A3A4A5A6"}
    devices = {"kb9": {"match": {"usb_serial": "0A0B0C0D0E0F1011"}}}
    said = {("1-1.3", "49"): {"chip_id": "0A0B0C0D0E0F1011", "serial_is_id": False}}
    assert usb_survey.watcher_owner(usb, devices, said) == ("kb9", "0A0B0C0D0E0F1011")
    assert usb_survey.watcher_owner(usb, devices, {("1-1.3", "48"): said[("1-1.3", "49")]}) == \
        (None, None), "another enumeration's answer is not this one's"
    assert usb_survey.watcher_owner(usb, {}, said) == (None, None)
