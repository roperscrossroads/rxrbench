#!/usr/bin/python
"""A board's bootloader, as declared: read it, decide, write it, read it back."""

DOCUMENTATION = r"""
module: bootloader_flash
short_description: Bring a board's bootloader to the declared one
description:
  - "Reads the bootloader a board holds and identifies it (C(module_utils/bootid.py): kind,
    signing key, build, exact hash), compares it with the staged one under a policy, and
    writes it only when the policy says so. Then reads it back and requires the identity to
    match, and requires a board that was running its app to run it again."
  - "C(RP2040 and RP2350 boards) (C(picoboot_mcuboot)): MCUboot at the start of flash, read and
    written through the chip's ROM bootloader (picotool, as the picoboot flasher reaches it), the
    read as long as the staged MCUboot so its hash compares; a write is whole 4 KB sectors, erased
    flash after the image. The board's app must answer again through it: back on USB as itself."
  - "C(ESP32 boards) reads and writes the bootloader partition with esptool
    over the wired route, the board's broker releasing the console. The header is written
    as built (C(--flash-mode keep)), so the board's bytes equal the file and a second run
    is a no-op. Refused unless the board's survey facts say secure boot and flash
    encryption are off and ROM download is on (the recovery path): run a deep survey first."
  - "Never part of C(apply): its own playbook (C(just boot)). Reading an ESP32's bootloader
    resets the board, so a plan (check mode) does not: it decides from the bootloader last
    read from the board (C(known), which the playbook keeps in the board's facts) and says the
    record is what it used, or that there is none. C(read) true makes a plan read the board."
options:
  board:
    description: The board's name.
    type: str
    required: true
  family:
    description: The firmware family the board runs (a family a plugin registers); how its app is
      read to tell that it answers again after the bootloader write.
    type: str
    required: true
  image:
    description: the staged image directory on this host (C(mcuboot.bin),
      C(flash.json), C(manifest.json)).
    type: path
  flasher:
    description: How the bootloader is read and written.
    type: str
    choices: [esp_mcuboot, picoboot_mcuboot]
    default: esp_mcuboot
  policy:
    description: C(key) writes when the board has no MCUboot or one with another signing key;
      C(exact) also when it is another build.
    type: str
    choices: [key, exact]
    default: key
  chip:
    description: The board's chip, as esptool names it (C(bench_mcu), from the board's type in catalog/boards.yml).
    type: str
  board_tag:
    description: The board type the registry declares; the staged image must be for it.
    type: str
  tags:
    description: The board's survey tags (its facts file, C(<facts>/<board>.yaml)).
    type: list
    elements: str
    default: []
  app_manager_overwrite:
    description: The layout's go-ahead (C(bench_app_manager_overwrite)) to write the bootloader
      of a chip whose partition table shows an app manager with images installed beside it (a
      plan reads it from the survey tags, the apply from the chip). Its bootloader is not
      MCUboot and every installed app depends on it, so the write is refused without it.
    type: bool
    default: false
  known:
    description: the bootloader identity last read from this board (its facts,
      C(probes.bootloader)), which a plan decides from instead of reading the board.
    type: dict
  read:
    description: read the board's bootloader in a plan too, which resets the
      board. A run that writes always reads.
    type: bool
    default: false
  chip_id:
    description: >-
      C(picoboot_mcuboot). The board's chip id (an RP2350's own, or an RP2040's flash id), as the
      registry gives it (C(bench_usb_serial)), by which picotool finds it in its ROM bootloader.
    type: str
  uf2:
    description: C(picoboot_mcuboot). The board type's C(uf2) block (C(catalog/boards.yml)).
    type: dict
  enter:
    description: C(picoboot_mcuboot). The ways into the ROM bootloader (C(bench_picoboot_enter)).
    type: list
    elements: str
  manual_wait:
    description: C(picoboot_mcuboot). Seconds to wait for someone to hold BOOTSEL, when C(manual) is a way in.
    type: float
    default: 0
  picotool:
    description: C(picoboot_mcuboot). The picotool on this host.
    type: path
    default: picotool
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  esptool:
    description: esptool on this host.
    type: path
    default: ~/.venv-esp/bin/esptool
  devreg:
    description: devreg on this host.
    type: path
    default: ~/.devreg/devreg.py
  settle:
    description: Seconds to wait for the app to answer after a reset.
    type: float
    default: 60.0
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode) needs none.
    type: bool
    default: false
  families:
    description: The device families as data, the inventory's C(bench_families). It says how
      a board's identity is read and what an image of a family must match.
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
"""

RETURN = r"""
before:
  description: The board's bootloader identity before the run.
  returned: always
  type: dict
after:
  description: The board's bootloader identity after a write.
  returned: changed
  type: dict
why:
  description: Why it was (or would be) written, or why not.
  returned: always
  type: str
read:
  description: whether the board's bootloader was read in this run (C(before) is
    then what the board holds, not a record).
  returned: always
  type: bool
"""

import json
import os
import tempfile
import time

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.module_utils import bootid, broker_client, esppart
from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, picoboot, picochip

REQUIRED_TAGS = ("secure-boot:off", "flash-encryption:off", "rom-download:on")


class EspMcuboot:
    def __init__(self, b, p):
        self.b, self.p = b, p
        self.name = p["board"]
        self.esptool, self.devreg = (os.path.expanduser(p[k]) for k in ("esptool", "devreg"))
        with open(os.path.join(p["image"], "flash.json")) as f:
            fj = json.load(f)
        boot = [i for i in fj["images"] if i.get("role") == "bootloader"]
        if len(boot) != 1:
            raise bootid.BootIdError("flash.json names no single bootloader image")
        self.offset = int(boot[0]["offset"], 16)
        self.file = os.path.join(p["image"], boot[0]["file"])
        self.size = int(fj.get("map", {}).get("BOOT_SIZE", "0x10000"), 16)
        self.chip = fj.get("chip") or p["chip"]
        if not self.chip:
            raise bootid.BootIdError("the board's chip is not known: neither the image's flash.json "
                                     "nor its type (catalog/boards.yml) names it")
        with open(os.path.join(p["image"], "manifest.json")) as f:
            self.manifest = json.load(f)
        with open(self.file, "rb") as f:
            self.want = bootid.identify(f.read())

    def refusals(self):
        out = []
        if self.offset != 0:
            out.append(f"the bootloader goes at {self.offset:#x}, not 0x0: not an ESP32 layout "
                       "this module knows")
        if os.path.getsize(self.file) > self.size:
            out.append(f"the bootloader ({os.path.getsize(self.file)} B) does not fit its "
                       f"partition ({self.size} B)")
        tag = self.manifest.get("board_tag")
        if self.p["board_tag"] and tag != self.p["board_tag"]:
            out.append(f"the image is for {tag}, the registry says {self.p['board_tag']}")
        missing = [t for t in REQUIRED_TAGS if t not in self.p["tags"]]
        if missing:
            out.append("the survey facts do not show " + ", ".join(missing) + " (run `just "
                       "survey <host> true` for this board first): a bootloader is only "
                       "written to an ESP32 whose ROM can recover it")
        # The survey's word on an app manager living on the chip: its bootloader is not
        # MCUboot and its installed apps need it. The read below asks the chip itself.
        why = common.manager_tag_refusal(self.p["tags"], self.p.get("app_manager_overwrite"),
                                         "writing the bootloader")
        if why:
            out.append(why)
        if not os.access(self.esptool, os.X_OK):
            out.append(f"no esptool at {self.esptool}")
        return out

    #: The app manager the last `read()` found in the chip's partition table (None: none).
    manager = None

    def _esptool(self, *args, timeout=120):
        port, why = common.esp_port(self.devreg, self.name, self.esptool, self.chip)
        if why:
            raise common.FlashError(why)
        return common.tool([self.esptool, "--chip", self.chip, "--port", port,
                            "--after", "hard-reset", *args], timeout)

    def read(self):
        """The bootloader the board holds. Resets the board; the console is released."""
        self.b.release(ttl=240)
        try:
            with tempfile.TemporaryDirectory() as d:
                out_file = os.path.join(d, "boot.bin")
                rc, out = self._esptool("read-flash", f"{self.offset:#x}", f"{self.size:#x}",
                                        out_file, timeout=180)
                if rc != 0 or not os.path.exists(out_file):
                    raise common.FlashError(f"esptool read-flash failed (rc {rc}): "
                                            + out.strip()[-300:])
                with open(out_file, "rb") as f:
                    data = f.read()
                # The same 64 KB holds the partition table at 0x8000: what the chip carries
                # is known from one read.
                self.manager = esppart.manager(esppart.parse(data, esppart.OFFSET))
                return bootid.identify(data)
        finally:
            self.b.reacquire()

    def write(self):
        self.b.release(ttl=300)
        try:
            rc, out = self._esptool("write-flash", "--flash-mode", "keep", "--flash-freq",
                                    "keep", "--flash-size", "keep", f"{self.offset:#x}",
                                    self.file, timeout=180)
            if rc != 0 or "Hash of data verified" not in out:
                raise common.FlashError(f"esptool write-flash failed (rc {rc}): "
                                        + out.strip()[-300:])
        finally:
            self.b.reacquire()


class PicobootMcuboot:
    """MCUboot at the start of an RP2040's or RP2350's flash, through its ROM bootloader."""
    manager = None

    def __init__(self, b, p):
        self.b, self.p = b, p
        self.name = p["board"]
        image = p["image"]
        with open(os.path.join(image, "manifest.json")) as f:
            self.manifest = json.load(f)
        with open(os.path.join(image, "flash.json")) as f:
            fmap = json.load(f).get("map") or {}
        self.file = os.path.join(image, self.manifest.get("file") or "mcuboot.bin")
        self.offset = int(self.manifest.get("offset") or "0x0", 16)
        self.size = int(fmap.get("BOOT_SIZE") or "0x10000", 16)
        with open(self.file, "rb") as f:
            self.data = f.read()
        self.want = bootid.identify(self.data, family="raw")
        self.rom = picoboot.Bootrom(self.name, p["chip_id"], p["uf2"] or {}, enter=p["enter"],
                                    manual_wait=p["manual_wait"], picotool=p["picotool"], devreg=p["devreg"])

    def refusals(self):
        out = []
        if self.offset != 0:
            out.append(f"the bootloader goes at {self.offset:#x}, not 0x0: not a layout this module knows")
        if len(self.data) > self.size:
            out.append(f"the bootloader ({len(self.data)} B) does not fit its partition ({self.size} B)")
        tag = self.manifest.get("board_tag")
        if self.p["board_tag"] and tag != self.p["board_tag"]:
            out.append(f"the image is for {tag}, the registry says {self.p['board_tag']}")
        if not self.p["chip_id"]:
            out.append("the board's chip id is not known (bench_usb_serial): picotool could not find it")
        if self.want.get("kind") != "mcuboot" or not self.want.get("key"):
            out.append("the staged file is not an MCUboot whose signing key bootid can read (ECDSA P-256)")
        return out

    def _rom(self):
        self.b.release(ttl=600)
        self.rom.enter()

    def _leave(self):
        try:
            self.rom.leave()
        finally:
            self.b.reacquire()

    def _read_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "boot.bin")
            self.rom.save(picochip.XIP + self.offset, picochip.XIP + self.offset + len(self.data), out)
            with open(out, "rb") as f:
                return f.read()

    def read(self):
        """The bootloader the board holds, over the staged one's length."""
        self._rom()
        try:
            return bootid.identify(self._read_bytes(), family="raw")
        finally:
            self._leave()

    def write(self):
        padded = self.data + b"\xff" * (-len(self.data) % picochip.SECTOR)
        with tempfile.TemporaryDirectory() as d:
            image = os.path.join(d, "mcuboot.uf2")
            picochip.sectors_uf2(padded, range(0, len(padded), picochip.SECTOR), picochip.XIP + self.offset,
                                 picochip.uf2_family(self.p["uf2"]), image)
            self._rom()
            try:
                self.rom.wrote = True
                rc, out = self.rom._pt("load", "-v", image, timeout=300)  # pylint: disable=protected-access
                if rc != 0:
                    raise common.FlashError(f"picotool load of MCUboot failed (rc {rc}): {out.strip()[-300:]}")
            finally:
                self._leave()

    def app_running(self):
        """The board's app on USB as itself (devreg)."""
        return common.guarded_port(self.p["devreg"], self.name)[1] is None

    def came_back(self, settle):
        """The app back on USB as itself within `settle` seconds, after the bootloader was left."""
        end = time.monotonic() + settle
        while time.monotonic() < end:
            if self.app_running():
                return True
            time.sleep(1.0)
        return False


def plan_from_record(want, known, policy):
    """(write?, why) for a plan that does not read the board: from the bootloader identity last
    read from it, or, with no record, a write that applying will decide once it has read."""
    if not (known or {}).get("kind"):
        return True, ("the board's bootloader is not known (nothing recorded): a plan does not "
                      "read it, which would reset the board. Applying reads it and writes only "
                      "if the policy says so; `read: true` makes a plan read it")
    write, why = bootid.decide(want, known, policy)
    return write, (f"{why} (the bootloader recorded {known.get('gathered') or 'earlier'}, not "
                   f"read now: a read resets the board)")


def esp_mcuboot(module, b, p):
    run(module, b, p, EspMcuboot(b, p))


def run(module, b, p, f):
    """Read, decide, write, read back, and the app answers again, for either flasher."""
    refused = f.refusals()
    if refused:
        module.fail_json(msg="refused: " + "; ".join(refused), want=f.want)
    if module.check_mode and not p["read"]:
        write, why = plan_from_record(f.want, p["known"], p["policy"])
        result = {"changed": write, "before": p["known"] or {}, "want": f.want, "why": why,
                  "read": False}
        if write:
            # not "unknown", which reads as a write that will happen: applying decides once it
            # has read the board
            result["diff"] = {"before": {"bootloader": p["known"] or "not read (applying reads it first and writes only if it differs)"},
                              "after": {"bootloader": f.want}}
        module.exit_json(**result)
    running = getattr(f, "app_running", None) or (lambda: common.app_running(b, p["family"]))
    back = getattr(f, "came_back", None) or (lambda settle: common.came_back(b, p["family"], settle))
    was_running = running()
    before = f.read()
    write, why = bootid.decide(f.want, before, p["policy"])
    result = {"changed": write, "before": before, "want": f.want, "why": why, "read": True}
    if was_running and not back(p["settle"]):
        module.fail_json(msg="the app did not come back after the bootloader was "
                             "read (left in ROM download mode?)", **result)
    if write:
        result["diff"] = {"before": {"bootloader": before},
                          "after": {"bootloader": f.want}}
    refused = common.manager_refusal(f.manager,
                                     p.get("app_manager_overwrite"), "writing the bootloader")
    if write and refused:
        module.fail_json(msg="refused: " + refused, **result)
    if not write or module.check_mode:
        module.exit_json(**result)
    f.write()
    after = f.read()
    result["after"] = after
    if after.get("sha256") != f.want["sha256"]:
        module.fail_json(msg="the bootloader read back is not the one written", **result)
    if was_running and not back(p["settle"]):
        module.fail_json(msg="bootloader written and verified, but the app did not "
                             "come back through it: write the app (just apply), or "
                             "recover in ROM download mode", **result)
    module.exit_json(**result)


def main():
    module = AnsibleModule(
        argument_spec={
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "family": {"type": "str", "required": True},
            "image": {"type": "path"},
            "flasher": {"type": "str", "choices": ["esp_mcuboot", "picoboot_mcuboot"],
                        "default": "esp_mcuboot"},
            "chip_id": {"type": "str"},
            "uf2": {"type": "dict"},
            "enter": {"type": "list", "elements": "str"},
            "manual_wait": {"type": "float", "default": 0},
            "picotool": {"type": "path", "default": "picotool"},
            "known": {"type": "dict"},
            "read": {"type": "bool", "default": False},
            "policy": {"type": "str", "choices": list(bootid.POLICIES), "default": "key"},
            "board_tag": {"type": "str"},
            "chip": {"type": "str"},
            "tags": {"type": "list", "elements": "str", "default": []},
            "app_manager_overwrite": {"type": "bool", "default": False},
            "socket": {"type": "path"},
            "esptool": {"type": "path", "default": "~/.venv-esp/bin/esptool"},
            "devreg": {"type": "path", "default": "~/.devreg/devreg.py"},
            "settle": {"type": "float", "default": 60.0},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    familyspec.load(p["families"])
    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            if p["flasher"] == "picoboot_mcuboot":
                run(module, b, p, PicobootMcuboot(b, p))
            else:
                esp_mcuboot(module, b, p)
    except (broker_client.BrokerError, common.FlashError, bootid.BootIdError, OSError,
            ValueError, KeyError) as e:
        module.fail_json(msg=str(e))


if __name__ == "__main__":
    main()
