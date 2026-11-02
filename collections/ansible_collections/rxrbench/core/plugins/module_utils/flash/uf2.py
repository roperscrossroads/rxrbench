"""The flasher for boards with a UF2 bootloader whose drive carries the board's serial (the
nRF52840's Adafruit bootloader and its forks, among others), over the wired route.

The write is the bench host's UF2 tool (`uf2_flash.py`, or the one a layout names): it checks
the UF2 (family, address range inside the application area, never a bootloader image), puts
the running app into UF2 mode, finds the drive by the chip's serial, copies, and waits for
the app's port to come back. This flasher adds what is common to every flash (the image is
for this board type and is what the layout declares; the port is the named board) and the
read-back.
"""
import json
import os
import re

from .. import familyspec
from . import common

#: The bench host's tool for any board with a UF2 bootloader (host/bin/uf2_flash.py).
DEFAULT_TOOL = "~/rxrbench/bin/uf2_flash.py"

FORMATS = ("uf2",)


def _entry(family):
    e = familyspec.of(family).get("uf2_enter") or {}
    if not (e.get("shell_command") or e.get("app_tools")):
        return None
    return {"family": family, "shell_command": e.get("shell_command"),
            "shell_usb": list(e.get("shell_usb") or []), "app_tools": list(e.get("app_tools") or [])}


def enter_entries(family, runs=None, enter_as=None):
    """The ways the UF2 tool may ask the board's app for UF2 mode, in order
    (docs/ARCHITECTURE.md#entering-uf2-mode): the family the board was read as running, alone; else the one
    the user named (`bench_uf2_enter_as`), alone; else every loaded family's, `family` (the
    image's) first. A family that says nothing of UF2 mode gives none, so a board read as
    running it is asked by nobody else's command."""
    if runs or enter_as:
        e = _entry(runs or enter_as)
        return [e] if e else []
    fams = [family] + sorted(f for f in familyspec.families() if f != family)
    return [e for e in (_entry(f) for f in fams) if e]


def tool_env(name, family, board_tag=None, runs=None, enter_as=None):
    """The environment uf2_flash.py (and a tool that drives it) is run with: the broker has let
    go of the console, the board's type (taken when the registry does not say), and the ways
    its app may be asked for UF2 mode (enter_entries), with the family it runs when known."""
    return {"RXRBENCH_RELEASED": name,
            **({"UF2_BOARD_TYPE": board_tag} if board_tag else {}),
            "UF2_ENTERS": json.dumps(enter_entries(family, runs, enter_as)),
            **({"UF2_RUNS": runs} if runs else {}),
            **({"UF2_ENTER_AS": enter_as} if enter_as and not runs else {})}


class Uf2Flasher(common.Flasher):
    def __init__(self, board, name, manifest, image_file, board_tag=None, declared=None,
                 uf2_tool=None, devreg="~/.devreg/devreg.py", deadline=10.0, settle=180.0,
                 bootloader_version=None, enter_as=None):
        self.b, self.name = board, name
        self.enter_as = enter_as    # the family the user says it runs (bench_uf2_enter_as)
        self.bootloader_version = bootloader_version   # what the survey read, for a first flash
        self.manifest, self.image = manifest, image_file
        self.board_tag, self.declared = board_tag, declared
        self.uf2_tool = os.path.expanduser(uf2_tool or DEFAULT_TOOL)
        self.devreg = os.path.expanduser(devreg)
        self.deadline, self.settle = deadline, settle

    def _env(self):
        return tool_env(self.name, self.manifest["family"], self.board_tag, self.running_family,
                        self.enter_as)

    def device(self):
        return common.read_board(self.b, self.manifest["family"], self.deadline)

    def preflight(self):
        out = common.common_refusals(self.manifest, FORMATS, self.board_tag, self.declared)
        if not os.access(self.uf2_tool, os.X_OK):
            out.append(f"no UF2 tool at {self.uf2_tool}")
        _port, why = common.guarded_port(self.devreg, self.name)
        if why:
            out.append(why)
            return out
        if self.running_family and self.enter_as and self.running_family != self.enter_as:
            out.append(f"it says it runs {self.running_family}, and bench_uf2_enter_as names "
                       f"{self.enter_as}: what the board says wins; correct or drop the variable")
            return out
        out += self.enter_refusals()
        return out

    def enter_refusals(self):
        """What the tool would refuse when it asks the app for UF2 mode (an ambiguous app,
        a family that says nothing of it), said by the tool itself (`enter-choice`, nothing
        sent), so a plan shows it before an apply."""
        if not os.access(self.uf2_tool, os.X_OK):
            return []
        rc, out = common.tool([self.uf2_tool, "enter-choice", self.name], timeout=60,
                              env=self._env())
        if rc == 0:
            return []
        return [f"{os.path.basename(self.uf2_tool)} enter-choice: " + out.strip()[-500:]]

    # ---- family switch: a board running another known family ---------------------------
    # A UF2 copy writes the application area only; the bootloader stays. What the image's
    # family needs of the bootloader is all a switch has to check, as for a first flash.
    def family_switch_refusals(self, before, check_mode=False):  # pylint: disable=unused-argument
        return self.first_flash_refusals()

    def write(self):
        self.b.release(ttl=self.settle + 240)
        # The tool refuses a board whose console a broker owns, unless told the broker has
        # let go of it: that is this.
        rc, out = common.tool([self.uf2_tool, "uf2", self.name, self.image], timeout=240,
                              env=self._env())
        if rc != 0 or "programmed; app up" not in out:
            raise common.FlashError(f"{os.path.basename(self.uf2_tool)} uf2 failed (rc {rc}): " + out.strip()[-400:])

    # ---- first flash: a board on other firmware (or its bootloader alone) --------------------
    # The image goes in by UF2 copy, as for an update; what is checked first is the
    # bootloader: it must be the one the family needs before an image reaches the board
    # (`just boot`).

    def _wanted_bootloader(self):
        """The bootloader the image's family needs under it, as a word its version carries
        (familyspec.py `uf2_enter.bootloader`), or None when any will do."""
        return (familyspec.of(self.manifest["family"]).get("uf2_enter") or {}).get("bootloader")

    def first_flash_refusals(self):
        want, v = self._wanted_bootloader(), self.bootloader_version
        if not want:
            return []
        if not v:
            return ["the board's bootloader version is not known: run `just boot` for it "
                    f"first (it reads it, and takes a factory bootloader to {want})"]
        if want not in v:
            return [f"the bootloader is {v}, not {want}: run `just boot` for it first"]
        return []

    def write_first(self):
        """Read the bootloader from inside UF2 mode, and write only if it is the one the
        family needs. What the facts say got the plan here; this is what the board says,
        right before the write."""
        want = self._wanted_bootloader()
        self.b.release(ttl=self.settle + 240)
        rc, out = common.tool([self.uf2_tool, "uf2-mode", self.name], timeout=120,
                              env=self._env())
        m = re.search(r"UF2 Bootloader (\S+)", out)
        if rc != 0 or not m:
            raise common.FlashError(f"could not read the bootloader in UF2 mode (rc {rc}): "
                                    + out.strip()[-300:])
        if want and want not in m.group(1):
            raise common.FlashError(f"the bootloader is {m.group(1)}, not {want}: nothing "
                                    "written (the board is left in UF2 mode; `just boot` it)")
        self.write()

    def verify(self):
        return common.wait_back(self.b, self.manifest["family"], self.settle, self.deadline)
