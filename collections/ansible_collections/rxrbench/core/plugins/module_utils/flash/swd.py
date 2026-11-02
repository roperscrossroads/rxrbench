"""The flasher for a microcontroller behind an SWD probe (an ST-Link, a CMSIS-DAP probe).

A device reached this way has no console and no port: the probe is the USB device, and it
says nothing about what is wired to it. So the two things every other flasher gets from a
port are got differently here:

- which probe: the registry names it (`reach.swd.probe`); `devreg resolve` finds it and
  refuses when it is absent or when two probes match, and every session hands openocd that
  probe by its USB location (`devreg resolve --busid`), so a second probe of the same kind on
  the host is never the one opened;
- which chip: the registry holds the chip's own unique id (`reach.swd.uid`), and the host
  tool (`host/bin/swd_chip.py`) reads it inside the same debug session as the work and stops
  there if it differs. That is this route's `devreg guard`.

What the device runs is the hash of its flash, over the image's length: an image with no
console cannot say what it is, so it is compared byte for byte. An image staged from an Intel
HEX or ELF file linked behind a bootloader carries the address it goes at (`base`); the host
tool hashes, writes and reads back from there (`--at`).

A chip whose firmware sleeps with its debug clock off cannot be reattached while it runs
(a Zephyr sample on an STM32F103, right after this flasher wrote it). When
the registry says the probe's reset line is wired to the chip (`reach.swd.nrst: true`), every
session connects under reset, which gets in whatever the firmware does.

A chip whose ranges are declared (`bench_flash_regions`, docs/ARCHITECTURE.md#partitions-and-the-write-rule) is
asked core's write rule before a write: this flasher reads each range's first bytes through the
probe (`read_range`) and says where the write goes (`write_ranges`).
"""
import hashlib
import json
import os
import re
import tempfile

from . import common
from .. import hwsurvey, identity

FORMATS = ("raw-bin",)
# A USB location as devreg prints it and openocd takes it (`adapter usb location`).
USB_LOCATION = re.compile(r"^\d+-\d+(\.\d+)*$")
# hwsurvey's probe model -> the probe kind the host tool drives.
PROBE_KINDS = {"stlink-v2": "stlink", "stlink-v2-1": "stlink", "stlink-v3": "stlink",
               "cmsis-dap": "cmsis-dap"}


class Probe:
    """A chip behind a debug probe: the probe the registry names, found by devreg, and the
    host tool run against it with the chip's unique id as the guard. The flasher writes
    through it; the whole-chip backup and restore (swdchip.py) read and write through it."""

    def __init__(self, name, probe, chip, uid, nrst=False, swd_chip="~/rxrbench/bin/swd_chip.py",
                 devreg="~/.devreg/devreg.py"):
        self.name, self.probe, self.chip, self.uid = name, probe, chip, (uid or "").lower()
        self.nrst = bool(nrst)
        self.swd_chip, self.devreg = os.path.expanduser(swd_chip), os.path.expanduser(devreg)

    def probe_kind(self):
        """((kind, USB location), None) for the registry's probe, found on this host, or
        (None, why)."""
        rc, out = common.tool(["python3", self.devreg, "resolve", self.probe], 30)
        ident = out.strip().splitlines()[-1].strip() if out.strip() else ""
        vid, _, pid = ident.partition(":")
        row = hwsurvey.KINDS.get((vid.lower(), pid.lower()))
        if rc != 0 or not row:
            return None, f"devreg cannot resolve the probe {self.probe}: {out.strip()[-160:]}"
        kind = PROBE_KINDS.get(row.get("probe"))
        if row.get("kind") != "swd-probe" or not kind:
            return None, f"{self.probe} ({ident}) is not an SWD probe this flasher drives"
        rc, out = common.tool(["python3", self.devreg, "resolve", "--busid", self.probe], 30)
        where = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else ""
        if not USB_LOCATION.match(where):
            return None, (f"devreg cannot say where on USB the probe {self.probe} is "
                          f"({out.strip()[-160:] or f'rc {rc}'}): this host's devreg may be "
                          "older than this checkout; bring it up to date with `just "
                          "hosts-setup <layout>`. Nothing was done")
        return (kind, where), None

    def call(self, *args, timeout=180, at=None):
        """One host-tool session: its JSON answer, or FlashError (Unanswered when the probe is
        there and the chip does not answer it)."""
        kind, why = self.probe_kind()
        if why:
            raise common.FlashError(why)
        (kind, where) = kind
        rc, out = common.tool(["python3", self.swd_chip, *args, "--chip", self.chip,
                               "--probe", kind, "--location", where, "--uid", self.uid]
                              + (["--srst"] if self.nrst else [])
                              + (["--at", str(at)] if at is not None else []), timeout)
        try:
            r = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise common.FlashError(f"swd_chip {args[0]} said nothing readable (rc {rc}): "
                                    + out.strip()[-300:]) from None
        if rc != 0 or "error" in r:
            why = f"{self.name}: {r.get('error') or out.strip()[-300:]}"
            # The probe is there and the chip is not, or does not answer it (a core asleep
            # with its debug clock off): nothing was read, so the board is skipped rather
            # than failed. The host tool says so as a field, not in its wording.
            if r.get("unanswered"):
                raise common.Unanswered(why)
            raise common.FlashError(why)
        return r


class SwdFlasher(Probe, common.Flasher):
    def __init__(self, name, manifest, image_file, probe, chip, uid, board_tag=None, nrst=False,
                 swd_chip="~/rxrbench/bin/swd_chip.py", devreg="~/.devreg/devreg.py"):
        Probe.__init__(self, name, probe, chip, uid, nrst, swd_chip, devreg)
        self.manifest, self.image = manifest, image_file
        self.board_tag = board_tag
        self.chip_facts = None
        with open(image_file, "rb") as f:
            data = f.read()
        self.length, self.image_hash = len(data), hashlib.sha256(data).hexdigest()

    def _chip(self, *args, timeout=180, at=None):
        """A session at the image's base (an image linked behind a bootloader), or at `at`."""
        return self.call(*args, timeout=timeout,
                         at=self.manifest.get("base") if at is None else at)

    def device(self):
        r = self._chip("sha", str(self.length))
        self.chip_facts = r
        return identity.make(self.manifest["family"], hash=r["sha256"])

    def preflight(self):
        out = common.common_refusals(self.manifest, FORMATS, self.board_tag, at_base=True)
        if not self.uid:
            out.append("the registry holds no unique id for this chip (reach.swd.uid): "
                       "nothing proves what is wired to the probe")
        if not os.path.isfile(self.swd_chip):
            out.append(f"no swd_chip at {self.swd_chip}")
        if self.manifest.get("hash") and self.manifest["hash"] != self.image_hash:
            out.append("the image file is not the one its manifest describes (hash differs)")
        size = (self.chip_facts or {}).get("flash_size")
        if size and self.length > size:
            out.append(f"the image is {self.length} bytes; the chip's flash is {size}")
        return out

    def read_range(self, start, length):
        """The chip's bytes at an absolute address, read through the probe."""
        fd, path = tempfile.mkstemp(suffix=".bin")
        os.close(fd)
        try:
            self._chip("read", path, str(length), at=start)
            with open(path, "rb") as f:
                return f.read()
        finally:
            os.unlink(path)

    def write_ranges(self):
        """Where the write goes: the image's base, else the start of the chip's flash."""
        base = self.manifest.get("base")
        start = int(str(base), 0) if base is not None else (self.chip_facts or {}).get("flash_base")
        if start is None:
            raise common.FlashError("where the chip's flash starts is not known (swd_chip said "
                                    "no flash_base): the write rule cannot place the write")
        return [(start, self.length)]

    def write(self):
        self._chip("write", self.image)

    def verify(self):
        return self.device()
