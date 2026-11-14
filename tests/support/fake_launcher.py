"""An app manager's serial console on a pty, as Launcher's `src/serial_console.cpp` has it.

- nothing typed is echoed; a line ends at `\\n` and `\\r` is ignored; a blank line gets no
  answer; an unknown command answers `ERR unknown command, ...`;
- actions answer one line, `OK ...` or `ERR ...`; `version` answers `Launcher <ver>`;
  `partitions` prints a table as a release does (a subtype named only for `ota_N`, `fat`,
  `spiffs`, `littlefs`, else hex: `0x20` is Launcher's own; sizes `20KB`, `3MB`), the otadata
  slot marked `<-- BOOT`, ending in a `Flash size:` line; `settings get` prints one JSON line (with the WebUI password in it); `settings set
  <json>` replaces it;
- a power-on starts a boot window: `Press the button to enter the Launcher!` every tick for
  `window` seconds, then the selected app boots and the console goes silent; `nav SelPress`
  during the window enters the menu (`Type 'help' for Serial commands.`) and the console
  serves (unless `selpress_lost`: the press is answered and dropped, as a build whose input
  task clears it does); the console serves every other command during the window too, and a
  window with `bootToApp` off (set during it, say) ends in the menu; `reboot` answers
  `OK rebooting` and boots the selected app at once (a software reset follows the otadata),
  the console silent until `power_on()`;
- `flash firmware <name> <size>` answers `READY <size>`, reads that many raw bytes in 2048-byte
  pieces (`ACK <written>/<size>` each), installs the app into the next free slot, labelled as
  Launcher labels it (`images.slot_label`: six lowercase letters and digits of the name, zero
  padded, a digit in sixth place when taken), selects it and answers `OK flashed, rebooting`;
  `flash overwrite` reuses the slot of that label.
"""
import json
import os
import pty

from ansible_collections.rxrbench.launcher.plugins.module_utils.images import next_slot_label, slot_label
import threading
import time
import tty

#: The settings Launcher reads as bools (`is<bool>()` in applySettingsFromRoot).
BOOL_SETTINGS = {"bootToApp", "DDLB", "dev", "autoConnect", "onlyBins", "askSpiffs", "autoBackup",
                 "noDotFiles", "LauncherKeyLvl"}

BANNER = "Press the button to enter the Launcher!"
#: What a release prints for a subtype it has no name for (serial_console.cpp names only
#: ota_N, fat, spiffs and littlefs).
PRINTED = {"nvs": "0x02", "ota": "0x00", "test": "0x20", "factory": "0x00", "coredump": "0x03"}
MENU = "Type 'help' for Serial commands."


class FakeLauncher:
    def __init__(self, version="beta-60a40c8", window=0.0, tick=0.05, apps=(), boot=None,
                 flash_size=0x400000, name="Cheap Yellow Display", has_version=True,
                 mac="02:00:00:00:00:02", selpress_lost=False, has_overwrite=True, busy=0.0,
                 banner_delay=0.0, self_update=None, update_to=None):
        self.version, self.window, self.tick = version, window, tick
        # a CYD's console answers before its boot screen prints the first banner
        self.banner_delay = banner_delay
        # Launcher's own update, installed as the app `launch`: it boots it and copies itself over
        # Launcher's partition for `self_update` seconds (None: it never says it finished), then
        # prints the table line and restarts as `update_to`. A power-on meanwhile is counted in
        # `resets_mid_copy`: on hardware it leaves a half-written Launcher.
        self.self_update, self.update_to = self_update, update_to
        self.copying_until = None
        self.resets_mid_copy = 0
        self.selpress_lost = selpress_lost   # the press is answered but the boot loop never sees it
        self.has_version = has_version       # releases up to 2.9.1 have no `version` command
        # `flash overwrite` came after 2.9.1 (Launcher ebb2649): a 2.9.1 console does not know it,
        # and its `flash firmware` for a label already there installs a new slot beside it
        self.has_overwrite = has_overwrite
        # A board behind a UART bridge (a CH340: no flow control): after each piece it writes
        # flash for `busy` seconds before it acknowledges, and bytes that arrive meanwhile are
        # lost (observed 2026-11-03 on a CH340 board under Launcher 2.9.1: 348160 of 898496 bytes
        # acknowledged, the rest overran)
        self.busy = busy
        self.dropped = 0
        self.table = [("nvs", "data", "nvs", 0x9000, 0x5000),
                      ("otadata", "data", "ota", 0xE000, 0x2000),
                      ("test", "app", "test", 0x10000, 0x150000)]
        self.flash_size = flash_size
        for i, (label, size) in enumerate(apps):
            self.table.append((label, "app", f"ota_{i}", self._next_offset(), size))
        self.boot = boot
        self.name = name
        self.settings = {"onlyBins": True, "bootToApp": True, "bootTimer": 4, "DDLB": True,
                         "wui_usr": "admin", "wui_pwd": "launcher", "dev": False,
                         "autoConnect": True, "wifi": [{"ssid": "lab-iot", "pwd": "hunter22"}],
                         f"{mac}-{name}": 3}          # Launcher's per-board entry: the chip's MAC
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self._slave = slave
        self.seen = []
        self.flashed = []
        self.reboots = 0
        self.state = "menu"           # window | menu | app
        self.window_until = 0.0
        self.line = ""
        self.raw = None                # (name, size, received, overwrite) while streaming
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if window:
            self.power_on()

    # -- the device -------------------------------------------------------------------
    def _next_offset(self):
        return max(off + size for _l, _t, _s, off, size in self.table)

    def apps(self):
        return [r for r in self.table if r[1] == "app" and r[2] != "test"]

    def power_on(self):
        if self.copying_until is not None:
            self.resets_mid_copy += 1
        self.state = "window"
        self.window_until = time.monotonic() + self.window
        self.banners_from = time.monotonic() + self.banner_delay

    def out(self, text):
        os.write(self.master, text.encode())

    def close(self):
        self.stop = True
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass

    # -- the console ------------------------------------------------------------------
    def _run(self):
        os.set_blocking(self.master, False)
        last_banner = 0.0
        while not self.stop:
            now = time.monotonic()
            if self.copying_until is not None and self.self_update is not None and now >= self.copying_until:
                self.copying_until = None
                self.table = [r for r in self.table if not r[0].startswith("launch")]
                self.version, self.boot = self.update_to or self.version, None
                self.out("Partition table updated: boot_repair=0 removed_ota=1\r\n")
                self.reboots += 1
                self.power_on()
            if self.state == "window":
                if now >= self.window_until:
                    if self.settings.get("bootToApp"):
                        self.state = "app"       # the selected app boots: silence
                    else:
                        self.state = "menu"      # nothing to boot into: the menu
                        self.out(MENU + "\r\n")
                elif now - last_banner >= self.tick and now >= getattr(self, "banners_from", 0.0):
                    self.out(BANNER + "\r\n")
                    last_banner = now
            try:
                data = os.read(self.master, 4096)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            except OSError:
                return
            if self.raw is not None:
                self._stream(data)
                continue
            for ch in data.decode(errors="replace"):
                if ch == "\r":
                    continue
                if ch == "\n":
                    line, self.line = self.line.strip(), ""
                    if line:
                        self.seen.append(line)
                        self._execute(line)
                        if self.raw is not None:
                            break
                else:
                    self.line += ch

    def _stream(self, data):
        name, size, got, overwrite = self.raw
        got += data
        self.raw = (name, size, got, overwrite)
        # one ACK per 2048-byte piece read, and one for the last, shorter one
        acked = min(len(got), size) if len(got) >= size else (len(got) // 2048) * 2048
        if acked > self.raw_acked:
            if self.busy:
                # the piece is written to flash first: nothing is read meanwhile, and what the
                # host sent in that time overran the UART (a pty would keep it: drop it here)
                time.sleep(self.busy)
                try:
                    self.dropped += len(os.read(self.master, 1 << 20))
                except (BlockingIOError, OSError):
                    pass
            self.out(f"ACK {acked}/{size}\r\n")
            self.raw_acked = acked
        if len(got) >= size:
            label = slot_label(name)
            if overwrite and any(r[0] == label for r in self.apps()):
                self.table = [r for r in self.table if r[0] != label]
            else:
                label = next_slot_label(name, {r[0] for r in self.table})
            n = len(self.apps())
            self.table.append((label, "app", f"ota_{n}", self._next_offset(),
                               -(-size // 0x10000) * 0x10000))
            self.flashed.append((label, got[:size]))
            self.boot = label
            self.raw = None
            self.out("OK flashed, rebooting\r\n")
            self.reboots += 1
            self.state = "app"
            if name == "launch" and (self.self_update is not None or self.update_to):
                # the new Launcher copies itself over its own partition: busy, no console
                self.copying_until = time.monotonic() + (self.self_update or 0.0) if self.self_update is not None \
                    else float("inf")

    def _execute(self, line):
        if self.state == "app":
            return                          # the app has the port: nothing Launcher-like answers
        words = line.split()
        cmd = words[0].lower()
        if self.state == "window" and cmd == "nav" and len(words) > 1 and words[1] == "SelPress":
            self.out("OK nav SelPress\r\n")
            if not self.selpress_lost:
                self.state = "menu"
                self.out(MENU + "\r\n")
            return
        if cmd == "version" and self.has_version:
            self.out(f"Launcher {self.version}\r\n")
        elif cmd == "whoami":
            self.out(self.name + "\r\n")
        elif cmd == "nav" and len(words) > 1:
            self.out(f"OK nav {words[1]}\r\n")
        elif cmd == "reboot":
            self.out("OK rebooting\r\n")
            self.reboots += 1
            self.state = "app"
        elif cmd == "partitions":
            self.out("== Partition table ==\r\n")
            for label, ptype, sub, off, size in self.table:
                mark = "  <-- BOOT" if ptype == "app" and label == self.boot else ""
                sub = PRINTED.get(sub, sub)
                self.out(f"{label:<16} type={ptype:<4} subtype={sub:<9} offset=0x{off:06X} "
                         f"size=0x{size:06X} ({human(size)}){mark}\r\n")
            free = self.flash_size - self._next_offset()
            if free > 0:
                self.out(f"{'<free>':<16} offset=0x{self._next_offset():06X} size=0x{free:06X} "
                         f"({human(free)})\r\n")
            self.out(f"Flash size: {human(self.flash_size)}, free total: {human(max(free, 0))}\r\n")
        elif cmd == "settings" and len(words) > 1 and words[1] == "get":
            self.out(json.dumps(self.settings, separators=(",", ":")) + "\r\n")
        elif line.startswith("settings set "):
            try:
                sent = json.loads(line[len("settings set "):])
                # Launcher reads each flag with ArduinoJson's is<bool>(): a 0 or 1 is not a
                # bool, so it is skipped (and still answered OK), as every release does
                self.settings.update({k: v for k, v in sent.items()
                                      if k not in BOOL_SETTINGS or isinstance(v, bool)})
                self.out("OK settings loaded\r\n")
            except ValueError:
                self.out("ERR invalid settings json\r\n")
        elif cmd == "wifi" and len(words) > 3 and words[1] == "add":
            self.settings["wifi"].append({"ssid": words[2].strip('"'), "pwd": words[3].strip('"')})
            self.out(f"OK saved network {words[2]}\r\n")
        elif cmd == "wifi" and len(words) > 2 and words[1] == "del":
            self.settings["wifi"] = [w for w in self.settings["wifi"] if w["ssid"] != words[2].strip('"')]
            self.out("OK network removed\r\n")
        elif cmd == "partition" and len(words) == 3 and words[1] == "delete":
            # serial_console.cpp handlePartitionCommand, as in 2.9.1
            if words[2] == "test":
                self.out("ERR cannot delete the Launcher partition\r\n")
            elif not any(r[0] == words[2] for r in self.apps()):
                self.out("ERR partition not found\r\n")
            else:
                self.table = [r for r in self.table if r[0] != words[2]]
                if self.boot == words[2]:
                    self.boot = None
                self.out("OK partition deleted\r\n")
        elif cmd == "flash" and len(words) == 4 and words[1] in ("firmware", "overwrite") and (
                words[1] == "firmware" or self.has_overwrite):
            size = int(words[3], 0)
            if size <= 0:
                self.out("ERR invalid size\r\n")
                return
            if self._next_offset() + size > self.flash_size and not (
                    words[1] == "overwrite" and any(r[0] == slot_label(words[2]) for r in self.apps())):
                self.out("ERR no space for app: image does not fit\r\n")
                return
            self.raw, self.raw_acked = (words[2], size, b"", words[1] == "overwrite"), 0
            self.out(f"READY {size}\r\n")
        elif cmd == "help":
            self.out("Serial commands: nav, reboot, partitions, flash, wifi, settings, version, whoami\r\n")
        else:
            self.out("ERR unknown command, type 'help' for command list\r\n")


def human(n):
    """A size as a release prints it (partitioner.cpp): MB, KB or bytes."""
    if n % (1 << 20) == 0:
        return f"{n >> 20}MB"
    if n % 1024 == 0:
        return f"{n >> 10}KB"
    return f"{n}B"
