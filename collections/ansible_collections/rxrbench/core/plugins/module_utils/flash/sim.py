"""The flasher for a simulated board (a plugin's firmware run as a process).

A sim board is a process: its "flash memory" for the application is its executable,
`<run>/<board>.exe`, and its pid is in `<run>/<board>.pid`. Flashing is what flashing a real
board is, step for step: the broker gives up the console, the image is put in place, the
board is reset, the broker takes the console back (on a new pty, as a real board may come
back on a new port), and the board is read until it answers.
"""
import os
import shutil
import signal
import time

from .. import broker_client, familyspec, identity, stateline
from . import common

FORMATS = ("sim-exe",)


class SimFlasher(common.Flasher):
    def __init__(self, board, name, run_dir, manifest, image_file, board_tag=None,
                 declared=None, deadline=10.0, settle=20.0):
        self.b, self.name, self.run = board, name, run_dir
        self.manifest, self.image = manifest, image_file
        self.board_tag, self.declared = board_tag, declared
        self.deadline, self.settle = deadline, settle

    def device(self):
        family = self.manifest["family"]
        if familyspec.of(family)["identity"] is None:
            # A family with no state line (a bare application) is known by its bytes: what
            # the board runs is the executable its process is, the way a chip behind a probe
            # is known by the hash of its flash.
            pid = self._pid()
            if pid is None:
                raise common.Unanswered(f"{self.name}: no running sim board in {self.run}")
            return identity.make(family, hash=common.sha256_of(f"/proc/{pid}/exe"))
        return common.read_board(self.b, family, self.deadline)

    def _pid(self):
        try:
            with open(os.path.join(self.run, f"{self.name}.pid")) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def family_switch_refusals(self, _before, check_mode=False):  # pylint: disable=unused-argument
        """A sim board is a process: it runs whichever family's executable it is given (and
        restarts with the options that executable takes, its plugin's simboard.py)."""
        return []

    def preflight(self):
        out = common.common_refusals(self.manifest, FORMATS, self.board_tag, self.declared)
        if not os.access(self.image, os.X_OK):
            out.append("the image is not an executable")
        if self._pid() is None:
            out.append(f"no running sim board named {self.name} in {self.run}")
        return out

    def write(self):
        pid = self._pid()
        self.b.release(ttl=self.settle + 30)
        exe = os.path.join(self.run, f"{self.name}.exe")
        shutil.copy2(self.image, exe + ".new")
        os.replace(exe + ".new", exe)
        os.kill(pid, signal.SIGTERM)
        # Not until the old image has stopped: taking the console back sooner finds the old
        # process still answering on its pty, and "what does the board run now" is then
        # answered by the image that was just replaced.
        end = time.monotonic() + 10
        while not self._stopped(pid):
            if time.monotonic() > end:
                raise common.FlashError(f"the sim board (pid {pid}) did not stop")
            time.sleep(0.02)

    @staticmethod
    def _stopped(pid):
        """Gone, or a zombie its parent has not collected yet."""
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return True

    def verify(self):
        end = time.monotonic() + self.settle
        why = "the board did not come back"
        while time.monotonic() < end:
            try:
                r = self.b.reacquire()
                if r.get("ok"):
                    return self.device()
                why = r.get("error") or why
            except (broker_client.BrokerError, stateline.StateError) as e:
                why = str(e)
            time.sleep(0.3)
        raise common.FlashError(f"after the flash: {why}")
