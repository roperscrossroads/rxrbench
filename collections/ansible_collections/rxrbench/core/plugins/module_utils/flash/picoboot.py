"""The flasher for a board whose chip has a USB bootloader in ROM that speaks PICOBOOT (the
Raspberry Pi RP2040 and RP2350), driven with `picotool`.

The ROM cannot be overwritten, so every board of these chips can always be brought back, and
the bootloader is the same whatever firmware the board runs: CircuitPython, MicroPython, a
Zephyr or NuttX application, QMK, a pico-sdk program. So nothing here asks the firmware what
it is. As for a chip behind a debug probe (swd.py), what the board runs is the hash of its
flash, read through the bootloader over exactly the addresses the image writes, and an image
is identified by the same hash over its own payload (`payload_hash`).

Which chip: the registry's `usb_serial` is the chip's id as its firmware reports it over USB:
on an RP2040 its flash chip's unique id (it has none of its own), on an RP2350 the chip id in
its OTP. picotool selects the device by it (`--ser`), and every session reads the id back and
stops if it differs: this route's `devreg guard`. The drive is never used (an RP2040's carries
another serial).

Getting into the bootloader is the firmware's business, so it is a list of ways, tried in
order (the board type's `picoboot.enter`, or the layout's):

    reset-interface  `picotool reboot -f -u`: an application that exposes picotool's reset
                     interface (pico-sdk stdio over USB, arduino-pico)
    touch-1200       open the application's serial port at 1200 baud (CircuitPython,
                     arduino-pico, many TinyUSB applications)
    shell:<command>  send <command> on the application's serial port (a shell that has one)
    manual           wait for someone to hold BOOTSEL and press reset (`manual_wait` seconds)

How long each takes is the board type's too (`uf2.enter_wait`, seconds to wait for the
bootloader after a way in, default 10; `uf2.ask_timeout`, seconds a touch or command may take
before it counts as failed, default 15), and a layout may override either.

A board found already in its bootloader is used as it is. Whatever way got it there, a board
this flasher put into its bootloader is rebooted into its application when the run ends
(`leave()`), in check mode too.
"""
import hashlib
import os
import re
import struct
import subprocess
import tempfile
import time

from . import common
from .. import identity

FORMATS = ("uf2",)
UF2_MAGIC = (0x0A324655, 0x9E5D5157, 0x0AB16F30)
FLAG_NOT_MAIN_FLASH = 0x00000001
FLAG_FAMILY = 0x00002000
ENTER_DEFAULT = ("reset-interface", "touch-1200")
#: How much of the flash one `picotool save` reads. picotool 2.1 slows down as the range grows,
#: so a long range is read in pieces this size (4 MB read whole at 109 KB/s, in 128 KB
#: pieces at 270 KB/s, the same bytes).
SAVE_PIECE = 128 * 1024
#: Seconds one piece may take (it takes about half a second).
SAVE_PIECE_TIMEOUT = 60

#: Seconds to wait for the bootloader after each way in (the board resetting and enumerating).
ENTER_WAIT = 10.0
#: Seconds an ask may take (open the port, touch it or send the command, close it): under one on
#: a board that works. A port can enumerate and still take no input (a firmware whose USB
#: receive path is broken): a write, or the close after it, then blocks without end, so each ask
#: runs in a child process killed at this limit.
ASK_TIMEOUT = 15.0


class Uf2Error(ValueError):
    pass


def uf2_blocks(path):
    """{address: bytes} of a UF2's main-flash payload, and the set of family ids it names.
    Refuses anything that is not a whole UF2 or that writes one address twice."""
    with open(path, "rb") as f:
        data = f.read()
    if not data or len(data) % 512:
        raise Uf2Error(f"{os.path.basename(path)} is not a UF2 file (size {len(data)})")
    blocks, fams = {}, set()
    for i in range(0, len(data), 512):
        m0, m1, flags, addr, size, _bno, _nb, fam = struct.unpack_from("<8I", data, i)
        (m_end,) = struct.unpack_from("<I", data, i + 508)
        if (m0, m1, m_end) != UF2_MAGIC:
            raise Uf2Error(f"{os.path.basename(path)} block {i // 512} has no UF2 magic")
        if flags & FLAG_NOT_MAIN_FLASH:
            continue
        if size > 476:
            raise Uf2Error(f"{os.path.basename(path)} block {i // 512} carries {size} bytes")
        if flags & FLAG_FAMILY:
            fams.add(fam)
        if addr in blocks:
            raise Uf2Error(f"{os.path.basename(path)} writes 0x{addr:x} twice")
        blocks[addr] = data[i + 32:i + 32 + size]
    if not blocks:
        raise Uf2Error(f"{os.path.basename(path)} writes nothing to main flash")
    return blocks, fams


def ranges(blocks):
    """The address ranges a payload covers, merged: [(lo, hi), ...] with hi exclusive."""
    out = []
    for addr in sorted(blocks):
        end = addr + len(blocks[addr])
        if out and addr <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((addr, end))
    return out


def payload_hash(blocks):
    """The image's identity: sha256 over its payload in address order. A device read over the
    same addresses hashes to this when it holds the image."""
    h = hashlib.sha256()
    for addr in sorted(blocks):
        h.update(blocks[addr])
    return h.hexdigest()


def board_hash(blocks, read):
    """The same hash over what the device holds: `read(lo, hi)` returns its bytes there."""
    h = hashlib.sha256()
    for lo, hi in ranges(blocks):
        chunk = read(lo, hi)
        if len(chunk) != hi - lo:
            raise common.FlashError(f"read 0x{lo:x}..0x{hi:x} gave {len(chunk)} bytes")
        for addr in sorted(a for a in blocks if lo <= a < hi):
            h.update(chunk[addr - lo:addr - lo + len(blocks[addr])])
    return h.hexdigest()


def refusals(blocks, fams, uf2_spec):
    """What the board type (`uf2: {family, app: [lo, hi]}`) says the image may not do."""
    out = []
    want = uf2_spec.get("family")
    want = {int(w) for w in (want if isinstance(want, (list, tuple)) else [want])} if want else set()
    if not want:
        out.append("the board type names no UF2 family (catalog boards.yml `uf2.family`)")
    elif not fams or not fams <= want:
        out.append(f"the image is UF2 family {sorted(hex(f) for f in fams) or 'none'}; this board "
                   f"type takes {sorted(hex(f) for f in want)}")
    app = uf2_spec.get("app")
    if not app or len(app) != 2:
        out.append("the board type names no flash region an image may write (`uf2.app`)")
    else:
        lo, hi = int(app[0]), int(app[1])
        r = ranges(blocks)
        if r[0][0] < lo or r[-1][1] > hi:
            out.append(f"the image writes 0x{r[0][0]:x}..0x{r[-1][1]:x}, outside 0x{lo:x}..0x{hi:x}")
    return out


class Bootrom:
    """One chip's ROM bootloader, through picotool: found by its id, reached by the board's ways
    in, read, and left again. The flasher (PicobootFlasher) and the whole-chip backup, restore
    and erase (picochip.py) are both this."""

    def __init__(self, name, chip_id, uf2_spec, enter=None, manual_wait=0, picotool="picotool",
                 devreg="~/.devreg/devreg.py", enter_wait=None, ask_timeout=None):
        self.name = name
        self.chip_id = (chip_id or "").upper()
        self.uf2_spec = uf2_spec or {}
        self.enter_ways = list(enter or self.uf2_spec.get("enter") or ENTER_DEFAULT)
        self.manual_wait = float(manual_wait or 0)
        self.picotool = picotool
        self.devreg = os.path.expanduser(devreg)
        # each: the layout's, else the board type's, else the default
        self.enter_wait = float(enter_wait or self.uf2_spec.get("enter_wait") or ENTER_WAIT)
        self.ask_timeout = float(ask_timeout or self.uf2_spec.get("ask_timeout") or ASK_TIMEOUT)
        self.entered = None          # how this run put the board in its bootloader, if it did
        self.last_entered = None     # the same, once it has been put back
        self.wrote = False
        self.chip_facts = None

    # ---- picotool, always for this chip ----------------------------------------------------
    #: what picotool says, exiting 0 (v2.1), when no device in BOOTSEL mode has the serial asked for
    NOT_FOUND = "No accessible RP-series devices"

    def _pt(self, *args, timeout=60):
        rc, out = common.tool([self.picotool, *args, "--ser", self.chip_id], timeout)
        if self.NOT_FOUND in out:
            rc = rc or 1     # picotool exits 0 having found nothing: a failure, not a success
        return rc, out

    def in_bootloader(self):
        """Is the chip in its bootloader now, and is it this chip? Reads the flash id."""
        rc, out = self._pt("info", "-d", timeout=20)
        if rc != 0 or "Device Information" not in out:
            return False
        # The chip's own id: an RP2040 has none and is known by its flash chip's (`flash id`);
        # an RP2350 has one in OTP (`chipid`), which its bootloader and applications give as
        # their USB serial. picotool selects the device by that id (`--ser`: it reads the flash
        # chip's unique id itself), so a device found is this chip; an id printed must agree.
        # An RP2040 whose flash is blank prints no `flash id` (picotool gives it with an image's
        # information): the selection is then the proof. An RP2350's OTP id is always printed.
        m = re.search(r"(?:chipid|flash id):\s*0x([0-9A-Fa-f]+)", out)
        blank_rp2040 = m is None and re.search(r"type:\s*RP2040", out) is not None
        if (m is None and not blank_rp2040) or (m and m.group(1).upper() != self.chip_id):
            raise common.FlashError(f"REFUSING: the bootloader picotool found says chip "
                                    f"{m.group(1) if m else '?'}, not {self.name} ({self.chip_id})")
        facts = {"chip_id": m.group(1).upper() if m else self.chip_id}
        for key, pat in (("type", r"type:\s*(\S+)"), ("revision", r"revision:\s*(\S+)"),
                         ("flash_size", r"flash size:\s*(\d+)K")):
            mm = re.search(pat, out)
            if mm:
                facts[key] = int(mm.group(1)) * 1024 if key == "flash_size" else mm.group(1)
        self.chip_facts = facts
        return True

    def _wait_bootloader(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.in_bootloader():
                return True
            time.sleep(0.5)
        return False

    def _app_port(self):
        """The application's serial port, through devreg (resolve, then guard)."""
        port, why = common.guarded_port(self.devreg, self.name)
        if why:
            raise common.FlashError(why)
        return port

    _ASK = """
import sys, time
import serial
port, how, command = sys.argv[1], sys.argv[2], sys.argv[3]
if how == "touch":
    s = serial.Serial(port, 1200, write_timeout=2)
    s.dtr = False
else:
    s = serial.Serial(port, 115200, timeout=0.5, write_timeout=2)
    s.write(b"\\r\\n")
    time.sleep(0.3)
    s.write(command.encode() + b"\\r\\n")
    time.sleep(0.5)
s.close()
"""

    def _ask(self, port, how, command=""):
        """Ask the application on `port` for its bootloader: a 1200-baud touch, or a shell
        command. Its errors are expected (the board resets under it); only a hang is not."""
        import sys  # pylint: disable=import-outside-toplevel
        rc, out = common.tool([sys.executable, "-c", self._ASK, port, how, command],
                              self.ask_timeout)
        if rc == 124 and "(killed after" in out:
            raise common.FlashError(f"its port took no input within {self.ask_timeout:.0f} s")

    def _touch(self, port):
        self._ask(port, "touch")

    def _shell(self, port, command):
        self._ask(port, "shell", command)

    def enter(self):
        """Bring the chip into its bootloader. Returns how ("already" when it was there)."""
        if self.in_bootloader():
            return self.entered or "already"
        tried = []
        for way in self.enter_ways:
            try:
                if way == "reset-interface":
                    self._pt("reboot", "-f", "-u", timeout=20)
                elif way == "touch-1200":
                    self._touch(self._app_port())
                elif way.startswith("shell:"):
                    self._shell(self._app_port(), way.split(":", 1)[1])
                elif way == "manual":
                    if self.manual_wait <= 0:
                        tried.append("manual (no manual_wait given)")
                        continue
                    if self._wait_bootloader(self.manual_wait):
                        self.entered = way
                        return way
                    tried.append(f"manual (nobody pressed BOOTSEL within {self.manual_wait:.0f} s)")
                    continue
                else:
                    tried.append(f"{way} (not a way this flasher knows)")
                    continue
            except common.FlashError as e:
                tried.append(f"{way} ({e})")
                continue
            if self._wait_bootloader(self.enter_wait):
                self.entered = way
                return way
            tried.append(f"{way} (no bootloader within {self.enter_wait:.0f} s)")
        raise common.Unanswered(f"{self.name}: could not bring it into its bootloader: "
                                + "; ".join(tried))

    def leave(self):
        """Back into its application, if this run put it in the bootloader or wrote it (a board
        found there, blank or held in BOOTSEL, starts what was just written)."""
        if (self.entered and self.entered != "already") or self.wrote:
            self._pt("reboot", timeout=20)
            self.last_entered = self.entered
            self.entered, self.wrote = None, False

    def save(self, lo, hi, dest, timeout=None):
        """The chip's flash from `lo` to `hi` into the file `dest` (the chip in its bootloader),
        read in pieces of SAVE_PIECE and joined: picotool 2.1's `save` slows down as its range
        grows (about 270 KB/s for 128 KB, 65 KB/s for a whole 8 MB chip; the same bytes in
        pieces come back identical), while the flash and the USB link do not. `timeout` is per
        piece. The file is written as `dest`.part and renamed when whole: a read that fails
        leaves no partial file under the name a backup is kept by."""
        piece, part = dest + ".piece.bin", dest + ".part"
        try:
            with open(part, "wb") as out:
                for a in range(lo, hi, SAVE_PIECE):
                    b = min(a + SAVE_PIECE, hi)
                    if os.path.exists(piece):     # a save that writes nothing must not pass the last piece off
                        os.unlink(piece)
                    rc, msg = self._pt("save", "-r", f"0x{a:x}", f"0x{b:x}", piece,
                                       timeout=timeout or SAVE_PIECE_TIMEOUT)
                    if rc != 0:
                        raise common.FlashError(f"picotool save 0x{a:x}..0x{b:x} (of 0x{lo:x}..0x{hi:x}) "
                                                f"failed: {msg.strip()[-300:]}")
                    try:
                        with open(piece, "rb") as f:
                            data = f.read()
                    except OSError as e:
                        raise common.FlashError(f"picotool save 0x{a:x}..0x{b:x} wrote no file: {e}") from e
                    if len(data) != b - a:
                        raise common.FlashError(f"picotool save 0x{a:x}..0x{b:x} gave {len(data)} bytes, "
                                                f"not {b - a}")
                    out.write(data)
            os.replace(part, dest)
        finally:
            for f in (piece, part):
                if os.path.exists(f):
                    os.unlink(f)

    def _read(self, lo, hi):
        fd, tmp = tempfile.mkstemp(suffix=".bin")
        os.close(fd)
        try:
            self.save(lo, hi, tmp)
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            os.unlink(tmp)


class PicobootFlasher(Bootrom, common.Flasher):
    def __init__(self, name, manifest, image_file, chip_id, uf2_spec, board_tag=None,
                 declared=None, enter=None, manual_wait=0, picotool="picotool",
                 devreg="~/.devreg/devreg.py", enter_wait=None, ask_timeout=None,
                 console_expected=False, console_elsewhere=False):
        super().__init__(name, chip_id, uf2_spec, enter=enter, manual_wait=manual_wait,
                         picotool=picotool, devreg=devreg, enter_wait=enter_wait,
                         ask_timeout=ask_timeout)
        self.manifest, self.image = manifest, image_file
        self.board_tag, self.declared = board_tag, declared
        self.blocks, self.fams = uf2_blocks(image_file)
        # the bench reaches the board by its USB console, unless the layout says it is reached
        # some other way (a UART adapter of its own)
        self.console_expected, self.console_elsewhere = console_expected, console_elsewhere

    # ---- the four steps ----------------------------------------------------------------------
    def device(self):
        self.enter()
        return identity.make(self.manifest["family"], hash=board_hash(self.blocks, self._read))

    def preflight(self):
        out = common.common_refusals(self.manifest, FORMATS, self.board_tag, self.declared)
        if not re.fullmatch(r"[0-9A-F]{16}", self.chip_id):
            out.append("the registry holds no flash unique id for this chip (match.usb_serial, "
                       "16 hex): nothing proves which chip picotool would write")
        if subprocess.run(["sh", "-c", f"command -v {self.picotool}"], capture_output=True).returncode:
            out.append(f"no {self.picotool} on this host")
        out += refusals(self.blocks, self.fams, self.uf2_spec)
        if self.manifest.get("hash") and self.manifest["hash"] != payload_hash(self.blocks):
            out.append("the image file is not the one its manifest describes (payload hash differs)")
        size = (self.chip_facts or {}).get("flash_size")
        if size and ranges(self.blocks)[-1][1] - 0x10000000 > size:
            out.append(f"the image reaches past the chip's {size // 1024} KB of flash")
        # A build whose shell is on a physical UART, written to a board the bench reaches by
        # its USB console, answers nowhere: only BOOTSEL held at power-up brought it back
        where = self.manifest.get("console")
        if self.console_expected and where in ("uart", "none") and not self.console_elsewhere:
            out.append(f"the image's shell is on {'a physical UART' if where == 'uart' else 'no console'}, "
                       "not the board's USB port the bench reaches it by: after the write nothing "
                       "would answer (and only BOOTSEL held at power-up brings the board back). Build "
                       "it with its USB console (Zephyr: -S cdc-acm-console), or declare "
                       "bench_console_elsewhere: true for this board")
        return out

    def write(self):
        self.enter()
        self.wrote = True
        rc, out = self._pt("load", "-v", self.image, timeout=600)
        if rc != 0 or "failed" in out.lower():
            raise common.FlashError(f"picotool load failed (rc {rc}): {out.strip()[-400:]}")

    def verify(self):
        return self.device()
