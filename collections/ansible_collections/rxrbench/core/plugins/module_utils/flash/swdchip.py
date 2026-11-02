"""A whole flash chip behind a debug probe (the `swd` flasher: an STM32 on an ST-Link, a chip on
a CMSIS-DAP probe): read it, write it back, erase it. What chip_backup, chip_restore and
chip_erase do for such a board.

The chip is found and checked by its own unique id, as for a flash (swd.Probe): the host tool
reads the id inside each session and stops there when it is not the registry's. A backup records
the id, and a restore is refused unless the backup was read from this very chip: a chip image
holds whatever the firmware stored (keys, config, calibration).

The whole flash is what the chip says it has (`swd_chip.py id`: its size, where it starts). A
restore writes the image back over all of it (the tool erases the pages it covers, programs and
verifies), then reads the whole chip again, which must hash to the backup. A chip that already
holds the backup is not written. Such a board has no console: nothing is asked of its firmware,
and the probe holds the chip only for the length of each session.
"""
import hashlib
import time

from . import chip_flow, common, swd


def probe_of(p):
    route = p.get("route") or {}
    missing = [k for k in ("probe", "chip", "uid") if not route.get(k)]
    if missing:
        raise common.FlashError(f"the board's swd route names no {', '.join(missing)} "
                                f"(the registry's reach.swd): nothing says what is wired to "
                                f"which probe")
    tools = p.get("tools") or {}
    return swd.Probe(p["board"], route["probe"], route["chip"], route["uid"],
                     nrst=route.get("nrst"),
                     swd_chip=tools.get("swd_chip") or "~/rxrbench/bin/swd_chip.py",
                     devreg=tools.get("devreg") or "~/.devreg/devreg.py")


def _size(chip_id):
    size = chip_id.get("flash_size")
    if not size:
        raise common.FlashError("the chip did not say how much flash it has (swd_chip id): "
                                "nothing is read by a guessed length")
    return int(size)


class _Session(chip_flow.ChipSession):
    """The probe holds the chip only for each call: entering asks the chip what it is (its
    size, where its flash starts), and the host tool stops there if it is not the registry's."""

    def __enter__(self):
        self.id = self.route.probe.call("id", timeout=120)
        self.size, self.base = _size(self.id), self.id.get("flash_base")
        return self

    def read(self, raw):
        probe = self.route.probe
        got = probe.call("read", raw, str(self.size), timeout=600, at=self.base)
        chip = {"uid": (got.get("uid") or self.id.get("uid") or probe.uid).lower(),
                "part": probe.chip, "flash_size": self.size, "flash_base": self.base,
                "idcode": self.id.get("idcode")}
        return self.size, chip, {"device_unread": "a chip behind a probe has no console"}

    def refusals(self, meta, size, raw):
        if size != self.size:
            return [f"the backup is {size} B, the chip has {self.size} B"]
        return []

    def differ(self, raw):
        probe = self.route.probe
        self.digest = chip_flow.sha256_file(raw)
        now = probe.call("sha", str(self.size), timeout=600, at=self.base)["sha256"]
        if now == self.digest:
            return False, {"msg": "the chip holds the backup already"}
        return True, {"msg": f"would write {self.size} B to chip {probe.uid} through the probe "
                             f"{probe.probe}"}

    def write(self, raw):
        probe = self.route.probe
        started = time.monotonic()
        probe.call("write", raw, timeout=900, at=self.base)
        after = probe.call("sha", str(self.size), timeout=600, at=self.base)["sha256"]
        if after != self.digest:
            raise common.FlashError("after the write, the chip does not hash to the backup")
        return {"write_seconds": round(time.monotonic() - started, 1),
                "msg": "written back through the probe; the chip hashes to the backup"}


    def identify(self):
        # entering asked the chip in a session that stops unless its unique id is the registry's
        return {"uid": (self.id.get("uid") or self.route.probe.uid).lower(),
                "part": self.route.probe.chip, "flash_size": self.size,
                "flash_base": self.base, "idcode": self.id.get("idcode")}, self.size

    def erase(self):
        probe = self.route.probe
        # the host tool's whole-chip erase (the chip's mass erase), in a session that reads the
        # unique id first and stops there unless it is the registry's
        probe.call("erase", timeout=600)
        now = probe.call("sha", str(self.size), timeout=600, at=self.base)["sha256"]
        if now != hashlib.sha256(b"\xff" * self.size).hexdigest():
            raise common.FlashError("after the erase, the chip's flash does not read erased")
        return {"msg": f"the whole flash erased through the probe {probe.probe}; it reads erased"}


class SwdChip(chip_flow.ChipRoute):
    """A whole chip behind a debug probe."""
    name = "swd"

    def prepare(self, verb):
        self.probe = probe_of(self.p)

    def plan_words(self):
        return f"would read {self.p['board']}'s whole flash through the probe {self.probe.probe}"

    def refusals(self, meta, size):
        was = ((meta.get("chip") or {}).get("uid") or "").lower()
        if not was:
            return ["the backup records no chip: nothing shows it was read from this one"]
        if was != self.probe.uid:
            return [f"the backup was read from chip {was}, this board is chip {self.probe.uid}"]
        return []

    def session(self, verb):
        return _Session(self, verb)


def backup(module, p):
    """chip_backup for a chip behind a probe."""
    chip_flow.backup(module, p, SwdChip(p))


def restore(module, p, path):
    """chip_restore for a chip behind a probe: written back unless it holds the backup already,
    and read again, which must hash to it."""
    chip_flow.restore(module, p, SwdChip(p), path)


def erase(module, p):
    """chip_erase for a chip behind a probe: the mass erase, then the whole flash hashed."""
    chip_flow.erase(module, p, SwdChip(p))
