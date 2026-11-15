"""The host tool that streams an app into Launcher over the serial console, against a scripted
port: the protocol's edges (a lost ACK, an ERR mid-stream, no READY, no Launcher, the boot
window) and how the port is opened."""
import importlib.util
import json

import pytest

from tests.support.launcher_rig import TOOL

BANNER = "Press the button to enter the Launcher!"


class Device:
    """What answers on the port: a Launcher (or an app, `launcher=False`), in its boot window
    for `banners` answers, dropping the ACKs numbered in `drop`, answering ERR instead of the
    ACK numbered `err_at`, refusing the flash when not `ready`."""

    def __init__(self, launcher=True, banners=0, drop=(), err_at=None, ready=True, selpress_lost=False,
                 reset_at=None, answers_in_window=False):
        self.launcher, self.banners, self.drop, self.err_at, self.ready = launcher, banners, set(drop), err_at, ready
        self.reset_at = reset_at   # the ACK number at which the board restarts into its app
        self.selpress_lost = selpress_lost
        # a real Launcher's console answers `version` inside its boot window as well
        self.answers_in_window = answers_in_window
        self.out, self.raw, self.got, self.seen, self.acks = b"", None, b"", [], 0

    def write(self, data):
        if self.raw is not None:
            self.got += data
            self.acks += 1
            size = self.raw
            if self.acks == self.err_at:
                self.out += b"ERR write failed\r\n"
                self.raw = None
                return
            if self.acks == self.reset_at:
                self.out += (b"ets Jul 29 2019 12:21:46\r\n\r\nrst:0x8 (TG1WDT_SYS_RESET),boot:0x13 "
                             b"(SPI_FAST_FLASH_BOOT)\r\nstatus=6 try=1\r\n")
                self.raw, self.launcher = None, False
                return
            n = size if len(self.got) >= size else (len(self.got) // 2048) * 2048
            if self.acks not in self.drop:
                self.out += f"ACK {n}/{size}\r\n".encode()
            if len(self.got) >= size:
                if getattr(self, "no_ok", False):     # Launcher 3.0.0 installing its own update
                    self.out += (b"App registry: save label=launch name=launch fat= spiffs= ok=1\r\n"
                                 b"Settings stored in NVS successfully\r\n"
                                 b"ESP-ROM:esp32s3-20210327\r\nrst:0xc (RTC_SW_CPU_RST),boot:0x8 "
                                 b"(SPI_FAST_FLASH_BOOT)\r\n")
                else:
                    self.out += b"OK flashed, rebooting\r\n"
                self.raw = None
            return
        for line in data.decode().split("\n"):
            if data.decode() == "\n" and (self.launcher or self.banners):   # an empty line, as Launcher answers it
                self.out += b"ERR unknown command, type 'help' for command list\r\n"
                return
            line = line.strip()
            if not line:
                continue
            self.seen.append(line)
            if line == "version":
                if self.banners:
                    if self.answers_in_window:      # the reply first, the window's banner after it
                        self.out += b"Launcher 2.9.1\r\n"
                    self.out += (BANNER + "\r\n").encode()
                    self.banners -= 1
                elif self.launcher:
                    self.out += b"Launcher beta-1\r\n"
            elif line == "nav SelPress":
                self.out += b"OK nav SelPress\r\n"
                if not self.selpress_lost:
                    self.launcher = True
                    self.out += b"Type 'help' for Serial commands.\r\n"
            elif line.startswith("settings set "):
                self.out += b"OK settings loaded\r\n"
                if '"bootToApp":false' in line:   # the window ends in the menu (a 0 is no bool:
                    # Launcher's is<bool>() skips it and the window runs on)
                    self.launcher, self.banners = True, 0
                    self.out += b"Type 'help' for Serial commands.\r\n"
            elif line.startswith("flash "):
                size = int(line.split()[3])
                if self.ready:
                    self.raw = size
                    self.out += f"READY {size}\r\n".encode()
                else:
                    self.out += b"ERR no space for app: image does not fit\r\n"


class FakeSerial:
    device = None
    opened = []

    def __init__(self):
        self.port = self.baudrate = self.timeout = self.dtr = self.rts = None

    def open(self):
        FakeSerial.opened.append((self.port, self.baudrate, self.dtr, self.rts))

    def write(self, data):
        FakeSerial.device.write(data)

    def flush(self):
        pass

    def read(self, n):
        d = FakeSerial.device
        out, d.out = d.out[:n], d.out[n:]
        return out

    def close(self):
        FakeSerial.opened.append("closed")


@pytest.fixture
def tool(monkeypatch):
    spec = importlib.util.spec_from_file_location("launcher_serial_edges", TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m.serial, "Serial", FakeSerial)
    FakeSerial.opened = []
    return m


def run(tool, device, tmp_path, capsys, data=b"\x33" * 5000, *args):
    FakeSerial.device = device
    f = tmp_path / "app.bin"
    f.write_bytes(data)
    rc = tool.main(["--port", "/dev/ttyFAKE", "--name", "camstr", "--file", str(f), "--wait", "1", *args])
    return rc, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_the_port_is_opened_at_115200_with_both_modem_lines_asserted(tool, tmp_path, capsys):
    rc, out = run(tool, Device(), tmp_path, capsys)
    assert rc == 0 and out["ok"] and out["sent"] == 5000 and out["lost_acks"] == 0
    assert FakeSerial.opened == [("/dev/ttyFAKE", 115200, True, True), "closed"]


def test_a_lost_ack_is_waited_out_and_counted_not_fatal(tool, tmp_path, capsys):
    d = Device(drop={2})
    rc, out = run(tool, d, tmp_path, capsys)
    assert rc == 0 and out["ok"] and out["sent"] == 5000 and out["lost_acks"] == 1
    assert d.got == b"\x33" * 5000 and out["reply"].startswith("OK flashed")


def test_an_err_mid_stream_stops_the_stream_and_is_the_reply(tool, tmp_path, capsys):
    rc, out = run(tool, Device(err_at=2), tmp_path, capsys)
    assert rc == 1 and out["ok"] is False and out["reply"] == "ERR write failed" and out["sent"] == 4096


def test_no_ready_means_nothing_is_sent(tool, tmp_path, capsys):
    d = Device(ready=False)
    rc, out = run(tool, d, tmp_path, capsys)
    assert rc == 1 and out["ok"] is False and out["sent"] == 0 and out["reply"].startswith("ERR no space")
    assert d.got == b""


def test_a_board_in_its_app_is_said_with_what_the_port_printed(tool, tmp_path, capsys):
    d = Device(launcher=False)
    rc, out = run(tool, d, tmp_path, capsys)
    assert rc == 1 and out["ok"] is False and out["reply"] == "no Launcher console within 1 s" and out["tail"] == []
    assert "flash" not in " ".join(d.seen)


def test_the_boot_window_is_answered_and_the_banner_kept_out_of_the_log(tool, tmp_path, capsys):
    d = Device(launcher=False, banners=1)
    rc, out = run(tool, d, tmp_path, capsys)
    assert rc == 0 and out["ok"]
    assert d.seen.count("nav SelPress") == 1 and d.seen.index("nav SelPress") < d.seen.index("flash firmware camstr 5000")


def test_the_app_part_is_cut_by_offset_and_length_and_an_empty_part_is_refused(tool, tmp_path, capsys):
    whole = b"\x00" * 100 + b"\x55" * 300 + b"\xff" * 50
    d = Device()
    rc, out = run(tool, d, tmp_path, capsys, whole, "--offset", "100", "--length", "300")
    assert rc == 0 and d.got == b"\x55" * 300 and "flash firmware camstr 300" in d.seen
    rc, out = run(tool, Device(), tmp_path, capsys, whole, "--offset", "1000")
    assert rc == 2 and out == {"ok": False, "reply": "an empty app part"}


def test_overwrite_reuses_the_slot_of_that_name(tool, tmp_path, capsys):
    d = Device()
    rc, _out = run(tool, d, tmp_path, capsys, b"\x44" * 100, "--overwrite")
    assert rc == 0 and "flash overwrite camstr 100" in d.seen


def test_the_lines_rest_where_the_board_type_says(tool, tmp_path, capsys):
    rc, out = run(tool, Device(), tmp_path, capsys, b"\x33" * 5000, "--modem-rest", "deasserted")
    assert rc == 0 and out["ok"]
    assert FakeSerial.opened == [("/dev/ttyFAKE", 115200, False, False), "closed"]


def test_a_board_that_loses_the_key_press_is_entered_through_its_settings(tool, tmp_path, capsys):
    dev = Device(banners=3, selpress_lost=True)
    rc, out = run(tool, dev, tmp_path, capsys, b"\x33" * 5000, "--entry", "settings")
    assert rc == 0 and out["ok"], out
    sets = [l for l in dev.seen if l.startswith("settings set")]
    assert sets == ['settings set {"bootToApp":false}', 'settings set {"bootToApp":true}'], "off for the boot, on again in the menu"
    assert "nav SelPress" not in dev.seen
    assert dev.seen.index('settings set {"bootToApp":true}') < dev.seen.index("flash firmware camstr 5000")


def test_the_key_press_entry_on_a_board_that_loses_it_finds_no_console(tool, tmp_path, capsys):
    dev = Device(banners=2, selpress_lost=True, launcher=False)
    rc, out = run(tool, dev, tmp_path, capsys)
    assert rc == 1 and not out["ok"] and "no Launcher console" in out["reply"]
    assert dev.seen.count("nav SelPress") == 1



def test_a_board_that_restarts_under_an_install_is_said_to_and_its_console_kept(tool, tmp_path, capsys):
    """A board that restarts mid-install into its boot app: the failure keeps the reset line, not
    only its app's last lines."""
    cap = tmp_path / "install.log"
    rc, out = run(tool, Device(reset_at=2), tmp_path, capsys, b"\x33" * 5000, "--capture", str(cap),
                  "--ack-wait", "0.3")
    assert rc == 1 and out["resets"] == ["TG1WDT_SYS_RESET"]
    assert "rst:0x8 (TG1WDT_SYS_RESET)" in cap.read_text() and "READY 5000" in cap.read_text()


def test_a_version_reply_inside_the_boot_window_is_not_the_menu(tool, tmp_path, capsys):
    """Launcher answers `version` while its boot window still runs (seen on a CYD): taking
    that reply for the menu left the window unanswered, bootToApp booted the app mid-stream."""
    d = Device(launcher=False, banners=2, answers_in_window=True)
    rc, out = run(tool, d, tmp_path, capsys)
    assert rc == 0 and out["ok"], out
    assert d.seen.index("nav SelPress") < d.seen.index("flash firmware camstr 5000")


def test_a_board_already_in_the_menu_is_entered_with_no_key_press(tool, tmp_path, capsys):
    d = Device()
    rc, out = run(tool, d, tmp_path, capsys)
    assert rc == 0 and out["ok"] and "nav SelPress" not in d.seen


class SelfUpdating(Device):
    """After `OK flashed`, the new Launcher copies itself over its partition: its port goes away
    with the reboot (`vanish` reads fail) and comes back, then it prints the table line."""

    def __init__(self, vanish=0, finishes=True, **kw):
        super().__init__(**kw)
        self.vanish, self.finishes, self.after = vanish, finishes, 0


def test_launchers_own_update_is_waited_for_on_the_port_the_tool_holds(tool, tmp_path, capsys, monkeypatch):
    dev = SelfUpdating(vanish=2)
    real = FakeSerial.read

    def read(self, n):
        d = FakeSerial.device
        if d.after and d.vanish:
            d.vanish -= 1
            raise tool.serial.SerialException("device reports readiness to read but returned no data")
        if d.after == 1 and not d.vanish and d.finishes:
            d.after = 2
            d.out += b"I (812) Partitioneer: Copying\r\nPartition table updated: boot_repair=0 removed_ota=1\r\n"
        chunk = real(self, n)
        if b"OK flashed" in chunk:
            d.after = 1                         # the tool has read it: the board reboots now
        return chunk
    monkeypatch.setattr(FakeSerial, "read", read)
    monkeypatch.setattr(FakeSerial, "close", lambda self: FakeSerial.opened.append("closed"))
    rc, out = run(tool, dev, tmp_path, capsys, b"\x33" * 5000, "--self-update-wait", "5")
    assert rc == 0 and out["self_update"]["seen"] and "removed_ota=1" in out["self_update"]["line"]
    assert FakeSerial.opened.count("closed") >= 2, "the port that went away with the reboot was opened again"


def test_a_self_update_that_never_says_it_finished_is_reported_unseen(tool, tmp_path, capsys):
    rc, out = run(tool, SelfUpdating(finishes=False), tmp_path, capsys, b"\x33" * 5000, "--self-update-wait", "0.5")
    assert out["ok"] and out["self_update"]["seen"] is False


def test_launcher_3_0_0s_own_update_with_no_ok_is_done_once_it_reboots_and_copies_itself(tool, tmp_path, capsys,
                                                                                         monkeypatch):
    """3.0.0 saves the `launch` slot and reboots into it without `OK flashed`; the copy's own
    line then says the update went in (as the T-Embed does)."""
    dev = SelfUpdating()
    dev.no_ok = True
    real = FakeSerial.read

    def read(self, n):
        d = FakeSerial.device
        chunk = real(self, n)
        if b"RTC_SW_CPU_RST" in chunk and d.after == 0:
            d.after = 1
            d.out += b"Display Red Stripe: Launcher Update\r\nPartition table updated: boot_repair=0 removed_ota=1\r\n"
        return chunk
    monkeypatch.setattr(FakeSerial, "read", read)
    rc, out = run(tool, dev, tmp_path, capsys, b"\x33" * 5000, "--self-update-wait", "5")
    assert rc == 0 and out["ok"] and "no OK" in out["reply"]
    assert out["self_update"]["seen"] and "removed_ota=1" in out["self_update"]["line"]


def test_an_app_install_that_reboots_with_no_ok_still_fails(tool, tmp_path, capsys, monkeypatch):
    """Only Launcher's own update may end without an OK: an app's install must say OK."""
    monkeypatch.setattr(tool, "DONE_WAIT", 0.5)
    dev = Device()
    dev.no_ok = True
    rc, out = run(tool, dev, tmp_path, capsys)
    assert rc == 1 and not out["ok"] and out["reply"].startswith("no OK within")
    assert out["resets"] == ["RTC_SW_CPU_RST"]
