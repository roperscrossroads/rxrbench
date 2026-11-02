"""The flasher for ESP32-S3 boards, over the wired route. Its checks:

- the port is the named board (devreg resolve, then devreg guard);
- the image is for this type of board, and is the class declared for it; the class is read
  from the image's own signed TLV as well, so a manifest that mislabels its bytes is refused;
- the write cannot reach the next slot or the config region (offsets from the image's
  flash.json, which the build derived from its own devicetree).

It writes the application slot only. The bench scripts rewrote the bootloader with every
flash, always the same file; a bootloader a build happens to produce is not flashed by
accident here. Updating one is a separate, gated action.
"""
import json
import os

from .. import bootid, familyspec
from . import chip, common

FORMATS = ("esp-parts",)


class EsptoolFlasher(common.Flasher):
    def __init__(self, board, name, manifest, image_file, board_tag=None, declared=None,
                 chip=None, psram=None,
                 esptool="~/.venv-esp/bin/esptool", devreg="~/.devreg/devreg.py",
                 imginfo="~/rxrbench/bin/imginfo.py", deadline=10.0, settle=180.0,
                 facts_tags=(), manager_overwrite=False, facts_chip=None):
        self.b, self.name = board, name
        # the survey's record of the chip (its esptool probe: partitions, gathered)
        self.facts_chip = facts_chip or {}
        # what the survey learned (the first-flash refusals read it), and whether the layout
        # lets a write orphan an app manager's apps
        self.facts_tags, self.manager_overwrite = tuple(facts_tags or ()), manager_overwrite
        self.manifest, self.image = manifest, image_file
        self.board_tag, self.declared = board_tag, declared
        # The board's chip (esptool's --chip) and PSRAM, from its type (catalog/boards.yml).
        self.chip, self.psram = chip, psram
        self.esptool, self.devreg, self.imginfo = (os.path.expanduser(p) for p in
                                                   (esptool, devreg, imginfo))
        self.deadline, self.settle = deadline, settle
        self.slot0 = None

    #: the check before the write has a spot form: the table and the overlapped slot alone
    spot_reads = ("before write",)

    def device(self):
        return common.read_board(self.b, self.manifest["family"], self.deadline)

    def depth_moments(self):
        return {"no_full_record": common.no_full_record(self.facts_chip)}

    def _flash_map(self):
        """The image's own regions (its flash.json map), for a chip with no partition table."""
        try:
            with open(os.path.join(os.path.dirname(self.image), "flash.json")) as f:
                return common.flash_map_regions(json.load(f))
        except (OSError, ValueError):
            return []

    def _layout(self):
        """(slot0, limit): where the app goes and the first byte it must not reach."""
        with open(os.path.join(os.path.dirname(self.image), "flash.json")) as f:
            fj = json.load(f)
        slot0 = int(next(i["offset"] for i in fj["images"] if i["role"] == "slot0"), 16)
        after = [int(v, 16) for k, v in fj["partitions"].items()
                 if k in ("slot1", "storage") and int(v, 16) > slot0]
        if not after:
            raise ValueError("no slot1 or storage offset above slot0")
        return slot0, min(after)

    def preflight(self):
        out = common.common_refusals(self.manifest, FORMATS, self.board_tag, self.declared)
        if not self.chip:
            out.append("the board's chip is not known: its type (the registry's board_tag) is "
                       "not in catalog/boards.yml")
        try:
            self.slot0, limit = self._layout()
            size = os.path.getsize(self.image)
            if self.slot0 + size > limit:
                out.append(f"the app ({size} B at {self.slot0:#x}) would reach {limit:#x}, the "
                           f"next slot or the config region")
        except (OSError, ValueError, KeyError, StopIteration) as e:
            out.append(f"the image's flash.json does not give the layout ({e}): refusing to "
                       f"write blind")
        # What a family's images also carry in their own bytes is checked against the manifest.
        check = familyspec.of(self.manifest["family"])["image"].get("bytes_check")
        if check:
            field = check["field"]
            got = common.image_field_from_bytes(self.imginfo, self.image, check["args"])
            if got is None:
                out.append(f"the image's {field} cannot be read from its signed TLV")
            elif got != self.manifest.get(field):
                out.append(f"the manifest says {field} {self.manifest.get(field)} but the "
                           f"image's bytes are {field} {got}")
        if not os.access(self.esptool, os.X_OK):
            out.append(f"no esptool at {self.esptool}")
        _port, why = common.guarded_port(self.devreg, self.name)
        if why:
            out.append(why)
        # The write rule over the survey's record of the chip: a plan reads nothing from the
        # chip, so this is the plan's word; the apply asks the chip itself before it writes.
        # A first flash erases the whole chip (first_flash_refusals asks for that).
        if self.slot0 is not None and not self.first:
            out += common.recorded_write_refusals(
                self.facts_chip, self.facts_tags, [(self.slot0, os.path.getsize(self.image))],
                common.overwrite_allowed(self.overwrite, self.manager_overwrite),
                "writing the app slot")
        return out

    # ---- the chip's own partition table, read in the same session as the write -----------
    # An app manager in its own partition keeps installed images in slots after it (where
    # this flasher's slot0 lies) and relies on the bootloader at 0x0. Both writes orphan
    # every app, so the table is read first, with the chip left in its loader for the write
    # that follows; a refusal boots the board and gives its console back.

    #: chip_blocks.py's tools on the bench host (flash/chip.py tools_of), when the module was
    #: given them: the write rule then reads the chip in one loader session.
    blocks_tools = None
    #: the regions the layout lets a write take (bench_overwrite)
    overwrite = ()

    def _blocks(self):
        return chip.blocks_if_installed(self.blocks_tools)

    def _refuse_writes(self, port, writes, what):
        """The write rule (common.esp_write_refusals), asked of the chip just before the write;
        a refusal boots the board and gives its console back."""
        why = common.esp_write_refusals(
            self.esptool, self.chip, port, writes,
            allowed=common.overwrite_allowed(self.overwrite, self.manager_overwrite),
            check_depth=(self.checked or {}).get("depth", "full"), what=what, blocks=self._blocks(),
            flash_map=self._flash_map())
        if why:
            try:
                chip.reset(self.esptool, port, self.chip)
            finally:
                self.b.reacquire()
            raise common.FlashError("refused: " + "; ".join(why))

    def write(self, erased=False):
        """The app into slot0. `erased`: the chip was just erased (a first flash), so there is
        no table left to ask."""
        self.b.release(ttl=self.settle + 240)
        port, why = common.esp_port(self.devreg, self.name, self.esptool, self.chip)   # again, now that it is free
        if why:
            raise common.FlashError(why)
        if not erased:
            self._refuse_writes(port, [(self.slot0, os.path.getsize(self.image))],
                                "writing the app slot")
        # A chip on its native USB-Serial-JTAG (303a:1001; in ROM
        # download mode the same face) is not reset by the RTS line esptool's default
        # `hard_reset` toggles: the flash is written and the chip stays in ROM, silent
        # (a V4 that had been in ROM mode: "no echo" for the whole settle).
        # The watchdog reset works on that face; a UART bridge (a V3's CP210x) keeps its RTS reset.
        face = common.usb_face(port)
        after = ["--after", "watchdog-reset"] if face and face[:2] == ("303a", "1001") else []
        rc, out = common.tool(
            [self.esptool, "--chip", self.chip, "--port", port, "--baud", "921600", *after,
             "write_flash", "--flash_size", "detect", "--flash_freq", "80m", "--flash_mode",
             "dio", f"{self.slot0:#x}", self.image], timeout=240)
        if rc != 0 or "Hash of data verified" not in out:
            raise common.FlashError(f"esptool failed (rc {rc}): " + out.strip()[-400:])

    # ---- family switch: a board running another known family ---------------------------
    # One MCUboot and one signing key for every family on these boards: the app slot is
    # all a switch writes, so the board's MCUboot must verify
    # the key the new image is signed with. Its bootloader is read to say so (which resets
    # the board); a different key needs `just boot` first, never a guess.

    def family_switch_refusals(self, before, check_mode=False):
        out = []
        try:
            with open(self._bootloader(), "rb") as f:
                want = bootid.identify(f.read())
        except (OSError, ValueError, KeyError) as e:
            return [f"no bootloader beside the image to compare signing keys with ({e})"]
        if check_mode:
            # A plan resets nothing. The signing-key check waits for the apply, which reads
            # the board's bootloader before it writes and refuses there.
            self.deferred = ("the board's bootloader is not read in a plan (the read resets "
                             "the board): applying reads it first and refuses a signing key "
                             "that differs")
            return out
        try:
            have = self._board_bootloader()
        except (common.FlashError, OSError) as e:
            return [f"the board's bootloader could not be read ({e})"]
        if want.get("kind") != "mcuboot" or not want.get("key"):
            out.append("the image's bootloader is not an MCUboot with a signing key")
        elif have.get("key") != want["key"]:
            out.append(f"the board's MCUboot verifies key {have.get('key')}, the "
                       f"{self.manifest['family']} image is for {want['key']}: bring the "
                       f"bootloader over first (just boot)")
        return out

    def _board_bootloader(self):
        self.b.release(ttl=120)
        try:
            port, why = common.esp_port(self.devreg, self.name, self.esptool, self.chip)
            if why:
                raise common.FlashError(why)
            path = os.path.join(os.path.dirname(self.image), ".board-bootloader.bin")
            with chip.booted_after(self.esptool, port, self.chip):
                rc, out = common.tool([self.esptool, "--chip", self.chip, "--port", port,
                                       "--after", "no-reset", "read-flash", "0x0", "0x10000",
                                       path], timeout=120)
                if rc != 0:
                    raise common.FlashError(f"esptool read-flash failed (rc {rc}): "
                                            + out.strip()[-200:])
            with open(path, "rb") as f:
                data = f.read()
            os.remove(path)
            return bootid.identify(data)
        finally:
            self.b.reacquire()

    # ---- first flash: a board that does not run the image's family ---------------------
    # A first flash erases the whole chip, since leftover flash from other firmware can break
    # the config file system's mount (docs/HARDWARE.md, "ESP32-S3"). That removes the bootloader too,
    # so MCUboot and the app are written together.
    REQUIRED_TAGS = ("secure-boot:off", "flash-encryption:off", "rom-download:on")

    def _bootloader(self):
        with open(os.path.join(os.path.dirname(self.image), "flash.json")) as f:
            fj = json.load(f)
        boot = [i for i in fj["images"] if i.get("role") == "bootloader"]
        if len(boot) != 1 or int(boot[0]["offset"], 16) != 0:
            raise ValueError("flash.json names no single bootloader at 0x0")
        return os.path.join(os.path.dirname(self.image), boot[0]["file"])

    def first_flash_refusals(self):
        out = common.recorded_write_refusals(
            self.facts_chip, self.facts_tags, common.WHOLE_CHIP,
            common.overwrite_allowed(self.overwrite, self.manager_overwrite), "erasing the chip")
        tags = set(self.facts_tags)
        missing = [t for t in self.REQUIRED_TAGS if t not in tags]
        if missing:
            out.append("the survey facts do not show " + ", ".join(missing) + ": run "
                       "`just survey <host> true` for this board before its first flash")
        want = f"psram:{self.psram}" if self.psram else None
        have = sorted(t for t in tags if t.startswith("psram:"))
        if want and have and want not in have:
            out.append(f"the registry says {self.board_tag}, which has {want}, but the chip "
                       f"reports {', '.join(have)}: the wrong board type for this image")
        if want and not have:
            out.append(f"no PSRAM size in the survey facts to confirm {self.board_tag}")
        try:
            self._bootloader()
        except (OSError, ValueError, KeyError) as e:
            out.append(f"no bootloader beside the image ({e})")
        return out

    def write_first(self):
        self.b.release(ttl=self.settle + 600)
        port, why = common.esp_port(self.devreg, self.name, self.esptool, self.chip)
        if why:
            raise common.FlashError(why)
        self._refuse_writes(port, common.WHOLE_CHIP, "erasing the chip")
        base = [self.esptool, "--chip", self.chip, "--port", port, "--baud", "921600"]
        rc, out = common.tool(base + ["--after", "no-reset", "erase-flash"], timeout=240)
        if rc != 0:
            raise common.FlashError(f"esptool erase-flash failed (rc {rc}): "
                                    + out.strip()[-300:])
        rc, out = common.tool(base + ["write-flash", "--flash-mode", "keep", "--flash-freq",
                                      "keep", "--flash-size", "keep", "0x0",
                                      self._bootloader()], timeout=240)
        if rc != 0 or "Hash of data verified" not in out:
            raise common.FlashError(f"esptool bootloader write failed (rc {rc}): "
                                    + out.strip()[-300:])
        self.write(erased=True)

    def verify(self):
        return common.wait_back(self.b, self.manifest["family"], self.settle, self.deadline)
