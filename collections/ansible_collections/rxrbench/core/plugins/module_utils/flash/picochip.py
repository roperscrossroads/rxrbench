"""A whole RP2040 or RP2350 flash chip, through its ROM bootloader: read it, write it back, erase
it. What chip_backup, chip_restore and chip_erase do for a board the picoboot flasher writes, and
region_erase for one config region.

The chip is found and checked by its id, as for a flash (picoboot.Bootrom): a backup records it,
and a restore is refused unless the backup was read from this very chip (a chip image holds
whatever the firmware stored: keys, config, a file system). The whole flash is the board
type's `uf2.app` range (catalog boards.yml). A restore writes only the 4 KB sectors that differ
from what the chip holds (the smallest unit the flash erases), then reads the whole chip again,
which must hash to the backup. The board is in its bootloader for all of it, so its firmware
cannot change its flash in the middle; it is rebooted into its application at the end.
"""
import hashlib
import os
import shutil
import struct
import sys
import tempfile

from .. import broker_client
from . import chip_flow, common, picoboot, region_flow

SECTOR = 4096
PAGE = 256


def sha256_file(path, opener=open):
    h = hashlib.sha256()
    with opener(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def span(uf2_spec):
    """(lo, hi): the whole flash, as the board type gives it."""
    app = (uf2_spec or {}).get("app")
    if not app or len(app) != 2:
        raise common.FlashError("the board type names no flash range (catalog boards.yml uf2.app)")
    return int(app[0]), int(app[1])


def differing_sectors(want, have):
    """The offsets of the 4 KB sectors where `want` and `have` (bytes of one length) differ."""
    return [o for o in range(0, len(want), SECTOR) if want[o:o + SECTOR] != have[o:o + SECTOR]]


def sectors_uf2(data, offsets, base, family, path):
    """A UF2 that writes exactly the sectors at `offsets` of `data` (all their pages, so a sector
    the bootloader erases is written whole)."""
    pages = [(o + p) for o in offsets for p in range(0, SECTOR, PAGE)]
    with open(path, "wb") as f:
        for n, off in enumerate(pages):
            head = struct.pack("<8I", 0x0A324655, 0x9E5D5157, 0x2000, base + off, PAGE, n,
                               len(pages), family)
            f.write(head + data[off:off + PAGE].ljust(476, b"\0") + struct.pack("<I", 0x0AB16F30))
    return len(pages)


def uf2_family(uf2_spec):
    fam = (uf2_spec or {}).get("family")
    fams = fam if isinstance(fam, (list, tuple)) else [fam]
    # the "absolute" family (an RP2350's) writes where its addresses say, whatever the image
    return int(0xE48BFF57 if 0xE48BFF57 in [int(f) for f in fams if f is not None] else fams[0])


def erase_range(chip, lo, hi, scratch):
    """[lo, hi) erased through the bootloader (picotool selects the chip by its id), then read
    back: every byte must be 0xFF."""
    chip.wrote = True
    rc, out = chip._pt("erase", "-r", f"0x{lo:x}", f"0x{hi:x}", timeout=max(600, (hi - lo) // 20000))
    if rc != 0:
        raise common.FlashError(f"picotool erase failed (rc {rc}): {out.strip()[-300:]}")
    back = os.path.join(scratch, f"erased-{lo:x}.bin")
    chip.save(lo, hi, back)
    with open(back, "rb") as f:
        data = f.read()
    if len(data) != hi - lo or data.count(0xFF) != len(data):
        raise common.FlashError(f"after the erase, 0x{lo:x}..0x{hi:x} does not read erased")


class Chip(picoboot.Bootrom):
    def facts(self):
        f = dict(self.chip_facts or {})
        f.setdefault("chip_id", self.chip_id)
        return f


def _chip(p):
    tools = p.get("tools") or {}
    return Chip(p["board"], p["chip_id"], p["uf2"], enter=p.get("enter"),
                manual_wait=p.get("manual_wait") or 0, enter_wait=p.get("enter_wait"),
                ask_timeout=p.get("ask_timeout"), picotool=tools.get("picotool") or "picotool",
                devreg=tools.get("devreg") or "~/.devreg/devreg.py")


class _Held:
    """The board's broker lets go of its port for the run (the ways in open it), the board is
    marked busy on its host, and both are undone at the end, the board rebooted first."""

    def __init__(self, p, chip):
        self.p, self.chip, self.broker = p, chip, None
        sock = p.get("socket") or broker_client.socket_path(p["board"])
        if os.path.exists(sock):
            self.broker = broker_client.Board(p["board"], path=sock)
        self.busy = common.busy(p["board"], keys=[chip.chip_id])

    def __enter__(self):
        self.busy.__enter__()
        if self.broker:
            self.broker.__enter__()
            self.broker.release(ttl=3600)
        return self

    def __exit__(self, *exc):
        try:
            self.chip.leave()
        finally:
            try:
                if self.broker:
                    try:
                        self.broker.reacquire()
                    finally:
                        self.broker.__exit__(None, None, None)
            finally:
                self.busy.__exit__(*exc)
        return False


class _ChipSession(chip_flow.ChipSession):
    """The chip in its ROM bootloader for the verb: the broker let go of the port and the board
    marked busy on the way in; rebooted and both undone on the way out (_Held)."""

    def __enter__(self):
        p = self.route.p
        self.chip = _chip(p)
        self.held = _Held(p, self.chip)
        self.held.__enter__()
        try:
            self.how = self.chip.enter()
        except BaseException:
            self.held.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        return self.held.__exit__(*exc)

    def read(self, raw):
        lo, hi = self.route.lo, self.route.hi
        self.chip.save(lo, hi, raw)
        return hi - lo, {"chip": self.route.p.get("chip"), **self.chip.facts()}, \
            {"base": f"0x{lo:x}", "entered": self.how}

    def differ(self, raw):
        lo = self.route.lo
        with open(raw, "rb") as f:
            self.want = f.read()
        self.now = os.path.join(os.path.dirname(raw), "now.bin")
        self.chip.save(lo, lo + len(self.want), self.now)
        with open(self.now, "rb") as f:
            self.diff = differing_sectors(self.want, f.read())
        n, total = len(self.diff), len(self.want) // SECTOR
        return bool(self.diff), {
            "sectors": n, "bytes": n * SECTOR, "written_blocks": n, "blocks": total,
            "msg": f"{n} of {total} sectors differ from the backup"
                   + ("" if self.diff else ": the chip holds it already")}

    def write(self, raw):
        lo = self.route.lo
        image = raw + ".uf2"
        sectors_uf2(self.want, self.diff, lo, uf2_family(self.route.p["uf2"]), image)
        self.chip.wrote = True
        rc, out = self.chip._pt("load", "-v", image, timeout=max(600, len(self.diff) * 2))
        if rc != 0:
            raise common.FlashError(f"picotool load failed (rc {rc}): {out.strip()[-300:]}")
        self.chip.save(lo, lo + len(self.want), self.now)
        if sha256_file(self.now) != hashlib.sha256(self.want).hexdigest():
            raise common.FlashError("after the write the chip does not hash to the backup")
        return {"msg": f"{len(self.diff)} sectors written; the chip hashes to the backup"}


    def identify(self):
        # every picotool call names the chip by its id (`--ser`): this is the registry's chip
        return {"chip": self.route.p.get("chip"), **self.chip.facts()}, \
            self.route.hi - self.route.lo

    def erase(self):
        lo, hi = self.route.lo, self.route.hi
        tmpd = tempfile.mkdtemp()
        try:
            erase_range(self.chip, lo, hi, tmpd)
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
        return {"msg": "the whole flash erased and read back erased: the board stays in its ROM "
                       "bootloader until it is flashed"}


class PicobootChip(chip_flow.ChipRoute):
    """An RP2040's or RP2350's whole flash through its ROM bootloader."""
    name = "picoboot"

    def prepare(self, verb):
        if not self.p.get("chip_id"):
            raise common.FlashError("the registry holds no chip id for this board (its "
                                    "usb_serial): nothing says which chip picotool would reach")
        self.lo, self.hi = span(self.p["uf2"])

    def plan_words(self):
        return (f"would read {self.p['board']}'s whole flash (0x{self.lo:x}..0x{self.hi:x}) "
                f"through its ROM bootloader")

    def refusals(self, meta, size):
        out = []
        got = str((meta.get("chip") or {}).get("chip_id") or "").upper()
        if got != str(self.p["chip_id"] or "").upper():
            out.append(f"the backup was read from chip {got or '?'}, this board is chip "
                       f"{self.p['chip_id']}: a chip image belongs to the chip it was read from")
        if meta.get("base") != f"0x{self.lo:x}" or size != self.hi - self.lo:
            out.append(f"the backup covers {meta.get('base')}+{size}, this board's flash is "
                       f"0x{self.lo:x}+{self.hi - self.lo}")
        return out

    def session(self, verb):
        return _ChipSession(self, verb)


def backup(module, p):
    """chip_backup through the ROM bootloader."""
    chip_flow.backup(module, p, PicobootChip(p))


def restore(module, p, path):
    """chip_restore: the sectors that differ written back, the whole chip read again and checked
    against the backup. Its plan reads the chip too, to say how many sectors differ."""
    chip_flow.restore(module, p, PicobootChip(p), path)


def erase(module, p):
    """chip_erase: the whole flash, read back erased. The board then has no application: the ROM
    bootloader stays up, and the next flash finds it there."""
    chip_flow.erase(module, p, PicobootChip(p))


# ---- the region level: the regions an image's flash map names --------------------------
#: where an RP2040's or RP2350's flash is mapped (an offset in a flash map is from here)
XIP = 0x10000000


def _image_flasher(p):
    """(manifest, a PicobootFlasher of the image the board must run): the image's own bytes
    are what tells whether it does."""
    manifest, image_file = common.load_image(p["image"])
    if manifest.get("format") != "uf2":
        raise common.FlashError(f"a picoboot board's config regions are read through its image's UF2; "
                                f"this image is {manifest.get('format')}")
    tools = p.get("tools") or {}
    return manifest, picoboot.PicobootFlasher(
        p["board"], manifest, image_file, p["chip_id"], p["uf2"], enter=p.get("enter"),
        manual_wait=p.get("manual_wait") or 0, picotool=tools.get("picotool") or "picotool",
        devreg=tools.get("devreg") or "~/.devreg/devreg.py", enter_wait=p.get("enter_wait"),
        ask_timeout=p.get("ask_timeout"))


def _runs(fl, manifest):
    """The board's bytes over the image's own addresses hash to the image: it runs it, so the
    image's flash map is the board's. Else refused: a map from another build may put the
    config regions elsewhere, and a backup read from the wrong place is the wrong bytes."""
    have = picoboot.board_hash(fl.blocks, fl._read)
    want = manifest.get("hash") or picoboot.payload_hash(fl.blocks)
    if have != want:
        raise common.FlashError("the board does not run this image (its flash over the image's "
                                "addresses differs), so this image's flash map may not be the board's")
    return have


class _Session(region_flow.Session):
    """One bootloader session: the board entered, shown to run the image, read and written,
    rebooted into its app at the end (_Held)."""

    def __enter__(self):
        r = self.route
        self.held = _Held(r.p, r.fl)
        self.held.__enter__()
        self.tmpd = None
        try:
            r.fl.enter()
            h = _runs(r.fl, r.manifest)
        except BaseException:
            self.held.__exit__(*sys.exc_info())
            raise
        self.tag = h[:8]
        self.chip = {"chip_id": r.fl.chip_id}
        self.device = {"family": r.manifest["family"], "hash": h,
                       "version": r.manifest.get("version")}
        self.tmpd = tempfile.mkdtemp()
        return self

    def __exit__(self, *exc):
        try:
            self.held.__exit__(*exc)
        finally:
            shutil.rmtree(self.tmpd, ignore_errors=True)
        return False

    def read(self, regions, out):
        for name, off, size in regions:
            self.route.fl.save(XIP + off, XIP + off + size, os.path.join(out, f"{name}.bin"))

    def plan(self, parts):
        """Only the 4 KB sectors that differ from what the chip holds are written."""
        todo = []
        for x in parts:
            with open(x["file"], "rb") as f:
                want = f.read()
            # picotool names a file's type by its extension: .bin for raw bytes
            now = os.path.join(self.tmpd, f"{x['name']}-now.bin")
            self.route.fl.save(XIP + x["offset"], XIP + x["offset"] + x["size"], now)
            with open(now, "rb") as f:
                diff = differing_sectors(want, f.read())
            if diff:
                todo.append(dict(x, want=want, sectors=diff))
        return todo, "would write " + ", ".join(
            f"{x['name']} ({len(x['sectors'])} of {len(x['want']) // SECTOR} sectors)" for x in todo)

    def write(self, parts, meta):
        fl = self.route.fl
        fl.wrote = True
        for x in parts:
            image = os.path.join(self.tmpd, f"{x['name']}.uf2")
            sectors_uf2(x["want"], x["sectors"], XIP + x["offset"], uf2_family(self.route.p["uf2"]),
                        image)
            rc, out = fl._pt("load", "-v", image, timeout=max(300, len(x["sectors"]) * 2))
            if rc != 0:
                raise common.FlashError(f"picotool load of {x['name']} failed (rc {rc}): "
                                        f"{out.strip()[-300:]}")
            back = os.path.join(self.tmpd, f"{x['name']}-back.bin")
            fl.save(XIP + x["offset"], XIP + x["offset"] + len(x["want"]), back)
            if sha256_file(back) != x["sha256"]:
                raise common.FlashError(f"after the write, {x['name']} does not hash to the backup")


    def erase(self, name, offset, size):
        """One config region erased, in the session that showed the board runs the image whose
        map puts it there, and read back erased."""
        erase_range(self.route.fl, XIP + offset, XIP + offset + size, self.tmpd)


class Picoboot(region_flow.Route):
    """An RP2040 or RP2350 through its ROM bootloader: the config regions its image's map names."""
    name = "picoboot"

    def prepare(self, verb):
        self.manifest, self.fl = _image_flasher(self.p)

    def chip(self):
        return {"chip_id": self.p.get("chip_id")}

    def session(self, verb, regions, name=None):
        return _Session(self, verb)
