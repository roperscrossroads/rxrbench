"""A simulated board's chip: its flash file.

A sim board's application is its executable (flash/sim.py); everything the firmware stores
(config, a key it made) is in the flash file the executable is given, `<run>/<board>.flash`.
So that file is the whole chip to back up, write back and erase, with the steps a real chip
gets: read with the board held still, written or erased with the board stopped and started
again on the new file (the bench's supervisor restarts a sim board that exits), the console
taken back when it answers.
"""
import contextlib
import os
import shutil
import signal
import time

from .. import broker_client
from . import chip_flow, common

CHIP = "native-sim"


class SimChip:
    def __init__(self, board, name, run_dir, settle=20.0):
        if not run_dir:
            raise common.FlashError(f"{name}: a sim board's run directory is not known (run_dir)")
        self.b, self.name, self.run, self.settle = board, name, run_dir, settle
        self.flash = os.path.join(run_dir, f"{name}.flash")

    def pid(self):
        try:
            with open(os.path.join(self.run, f"{self.name}.pid")) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError) as e:
            raise common.Unanswered(f"{self.name}: no running sim board in {self.run} ({e})") from e

    def info(self):
        """What the chip is, in the words a real chip's record uses: no MAC (a board is one
        process, named), the flash file's size."""
        size = os.path.getsize(self.flash) if os.path.exists(self.flash) else 0
        return {"chip": CHIP, "mac": f"sim:{self.name}", "flash_size": f"{size}B"}

    def read(self, dest):
        """The flash file copied to `dest` with the board held still (SIGSTOP), so the copy is
        of one moment; the board goes on as if nothing happened."""
        pid = self.pid()
        if not os.path.exists(self.flash):
            raise common.FlashError(f"{self.name}: no flash file at {self.flash}: the image takes none, or "
                                    "the board is an emulated machine whose flash is its RAM (nothing to read; "
                                    "a power cycle gives it its factory image back)")
        os.kill(pid, signal.SIGSTOP)
        try:
            shutil.copyfile(self.flash, dest)
        finally:
            os.kill(pid, signal.SIGCONT)
        return os.path.getsize(dest)

    def write(self, src):
        """The flash file replaced by `src`, then the board restarted on it."""
        shutil.copyfile(src, self.flash + ".new")
        self._restart(lambda: os.replace(self.flash + ".new", self.flash))

    def erase(self):
        """The flash file removed, then the board restarted: it makes a blank one."""
        self._restart(lambda: os.path.exists(self.flash) and os.remove(self.flash))

    def _restart(self, change):
        pid = self.pid()
        self.b.release(ttl=self.settle + 30)
        # the change first, the stop after: the supervisor starts the new process the moment
        # the old one is gone, and it must open the new file (the old process keeps the old
        # inode mapped; what it writes on its way out goes nowhere that matters)
        change()
        os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + 10
        while not self._stopped(pid):
            if time.monotonic() > end:
                raise common.FlashError(f"the sim board (pid {pid}) did not stop")
            time.sleep(0.02)
        self._back()

    @staticmethod
    def _stopped(pid):
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return True

    def _back(self):
        end = time.monotonic() + self.settle
        why = "the board did not come back"
        while time.monotonic() < end:
            try:
                r = self.b.reacquire()
                if r.get("ok"):
                    return
                why = r.get("error") or why
            except broker_client.BrokerError as e:
                why = str(e)
            time.sleep(0.3)
        raise common.FlashError(f"after the write: {why}")


class _Session(chip_flow.ChipSession):
    """The board's broker held for the session; a write restarts the board on the new file."""

    def __enter__(self):
        p = self.route.p
        self.stack = contextlib.ExitStack()
        b = self.stack.enter_context(
            broker_client.Board(p["board"], path=p["socket"], timeout=p["settle"] + 120))
        self.chip = SimChip(b, p["board"], p["run_dir"], p["settle"])
        return self

    def __exit__(self, *exc):
        self.stack.close()
        return False

    def read(self, raw):
        size = self.chip.read(raw)
        return size, self.chip.info(), {}

    def differ(self, raw):
        if os.path.exists(self.chip.flash) and \
                chip_flow.sha256_file(self.chip.flash) == chip_flow.sha256_file(raw):
            return False, {"msg": "the flash file holds the backup already"}
        return True, {"msg": f"would be written to {self.route.p['board']}'s flash file"}

    def write(self, raw):
        self.chip.write(raw)
        if chip_flow.sha256_file(self.chip.flash) != chip_flow.sha256_file(raw):
            raise common.FlashError("after the write, the flash file does not hash to the backup")
        return {"msg": f"written back to {self.route.p['board']}'s flash file; the board "
                       "restarted on it"}


    def identify(self):
        self.chip.pid()   # a board that is not running is not there to erase
        flash = self.chip.flash
        return self.chip.info(), os.path.getsize(flash) if os.path.exists(flash) else 0

    def erase(self):
        had_file = os.path.exists(self.chip.flash)
        self.chip.erase()
        if not had_file:
            # an emulated machine: its flash was its RAM, gone with the process; the board
            # starts again on the image staged for it
            return {"msg": "the board power-cycled: its flash was the machine's RAM, and it "
                           "starts on its factory image"}
        return {"msg": f"the flash file erased: {self.route.p['board']} starts on a blank one"}


class SimRoute(chip_flow.ChipRoute):
    """A simulated board's chip, whatever flashes it. Its backups record no route (they never
    did): another route's backup is refused by its chip, which is not a sim's."""
    name = None
    takes_unrouted = True

    def plan_words(self):
        return f"would read {self.p['board']}'s flash file"

    def refusals(self, meta, size):
        was = (meta.get("chip") or {}).get("chip")
        if was != CHIP:
            return [f"the backup is of a {was} chip, this board is a sim"]
        return []

    def session(self, verb):
        return _Session(self, verb)


def backup(module, p):
    """chip_backup on a sim board: the record shaped as a real chip's."""
    chip_flow.backup(module, p, SimRoute(p))


def restore(module, p, path):
    """chip_restore on a sim board: the recorded flash file written back, the board restarted
    on it. Refused for another board's backup, another chip's or a damaged one."""
    chip_flow.restore(module, p, SimRoute(p), path)


def erase(module, p):
    """chip_erase on a sim board: the whole flash file (everything the firmware stores), the
    board restarted on a blank one."""
    chip_flow.erase(module, p, SimRoute(p))
