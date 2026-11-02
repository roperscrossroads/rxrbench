"""The flasher for a whole ESP32 image: a release's merged `.bin`, linked for 0x0, that brings
its own bootloader, partition table, application and data partitions (the file an ESP-IDF
`merge-bin` or a vendor's web installer writes). The chip becomes the image, and 0xFF after it.

A release may publish more than the merged file: images of its own partitions (another app
for the second OTA slot, a file system). Staged with it (`stage_raw.py --part LABEL=file`), they
are laid into the merged file at their partitions' offsets, and the staged file is the chip as a
full install leaves it, up to its last written byte (0xFF after it).

What the board runs is not asked of its firmware (it may have no console this tool speaks):
it is read from the chip through the ROM loader, as the chip backups are. The loader's stub
hashes flash itself, fast, so the image's regions are compared hash for hash
(`chip_blocks.py match`), never read out:

- `boot`   0x0 up to the partition table: the second-stage bootloader
- `table`  the partition table (0x8000, 0xC00 bytes)
- each `app` partition of the image's own table that the image fills, as long as the app image
  there is (its header and segments say), or up to where the file ends

The table and the apps are the board's identity, with the app the chip boots: otadata says
which ota slot that is, and a board whose otadata selects another slot than the image's (one
left in an OTA loader the release ships in its second slot) does not run the image, whatever
its apps hold. otadata is compared by the slot it selects, not byte for byte: every OTA raises
its sequence. The bootloader is compared and reported, not held to: a vendor's own update
writes the apps only, so a board updated that way keeps the bootloader it had and still runs
the release. The other data partitions (nvs, phy_init, FAT, SPIFFS, LittleFS) are not compared
at all: the firmware writes them as it runs.

Two ways to write, the two a vendor's web installer offers:

- an **update** (a board on the image's partition table whose apps differ, or that boots
  another slot): only the app partitions that differ are written, each with the image's bytes,
  and the image's otadata when the board boots another slot, each checked by the stub's hash
  after. The bootloader, the table and every other data partition (the firmware's config, its
  identity key) are kept, as the installer's update leaves them;
- a **full install** (a first flash: the table is another firmware's; or the layout's
  `full_erase`): the whole chip becomes the staged file and 0xFF after it, every data partition
  as the image has it, so what the firmware kept there is gone (`just backup` first).

Its checks:

- the chip is the board: its MAC, read by the loader in the same session as the write, must be
  the registry's (`policy.chip_mac`, or a `usb_serial` that is the MAC). A board behind a
  bridge with no serial (a CH340) is otherwise one device to devreg for every such board;
- the image is an ESP32 merged image for this board type (a partition table at 0x8000, the
  bootloader's magic where this chip's ROM looks for it) and fits the chip;
- an app manager on the chip with apps beside it is not written over unless the layout says
  `bench_app_manager_overwrite` (the shared ESP32 refusal);
- a chip whose partition table is not the image's runs other firmware: writing it is a first
  flash (`bench_first_flash: true`), with the survey's security and PSRAM checks.

A full install is written block by block (`chip_blocks.py place`): a block that already holds
the image's bytes is not written again. An update is `chip_blocks.py region-update`, in the
same loader session as the check that the chip's table is the image's.
"""
import functools
import os
import struct

from .. import esppart, identity
from . import chip as chipio
from . import common

FORMATS = ("raw-bin",)
#: Where each chip's ROM loads the second-stage bootloader from (the merged image's padding
#: before it is 0xFF).
BOOT_OFFSET = {"esp32": 0x1000, "esp32s2": 0x1000}
IMAGE_MAGIC = 0xE9
REQUIRED_TAGS = ("secure-boot:off", "flash-encryption:off", "rom-download:on")


def app_length(blob, off, limit):
    """How long the ESP-IDF app image at `off` is (header, segments, checksum, the SHA-256 it may
    carry), or None when the bytes there are not one that ends before `limit`."""
    if off + 24 > limit or blob[off] != IMAGE_MAGIC:
        return None
    count, hashed = blob[off + 1], blob[off + 23]
    pos = off + 24
    for _ in range(count):
        if pos + 8 > limit:
            return None
        pos += 8 + struct.unpack_from("<I", blob, pos + 4)[0]
    pos += 16 - (pos - off) % 16      # the checksum byte ends a 16-byte line
    if hashed == 1:
        pos += 32
    return pos - off if pos <= limit else None


def regions(blob):
    """[(name, lo, hi)]: the regions of a merged image that are compared (module docstring)."""
    out = [("boot", 0, esppart.OFFSET), ("table", esppart.OFFSET, esppart.OFFSET + esppart.LENGTH)]
    for row in esppart.parse(blob, esppart.OFFSET):
        if row["type"] == esppart.APP and row["offset"] < len(blob):
            end = min(row["offset"] + row["size"], len(blob))
            if blob[row["offset"]] == 0xFF:          # a slot the image leaves empty
                continue
            n = app_length(blob, row["offset"], end)
            out.append((row["label"] or f"app@{row['offset']:#x}", row["offset"],
                        row["offset"] + n if n else end))
    return out


def identity_regions(blob):
    """The regions a board must hold to run the image: the table and the apps."""
    return [r for r in regions(blob) if r[0] != "boot"]


def app_regions(blob):
    """What an update writes: the image's app partitions."""
    return [r for r in identity_regions(blob) if r[0] != "table"]


def image_refusals(blob, chip):
    """What makes `blob` not an ESP32 merged image this chip's ROM would boot."""
    out = []
    rows = esppart.parse(blob, esppart.OFFSET)
    if not rows:
        out.append("the image has no partition table at 0x8000: not a merged image linked for 0x0")
    elif not any(r["type"] == esppart.APP for r in rows):
        out.append("the image's partition table has no app partition")
    boot = BOOT_OFFSET.get((chip or "").lower().replace("-", ""), 0x0)
    if len(blob) <= boot or blob[boot] != IMAGE_MAGIC:
        out.append(f"no bootloader image (magic 0x{IMAGE_MAGIC:02X}) at {boot:#x}, where a "
                   f"{chip or '?'} loads it from")
    return out


def norm_mac(mac):
    m = (mac or "").lower().replace(":", "").replace("-", "")
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if len(m) == 12 else None


class EspImageFlasher(common.Flasher):
    def __init__(self, name, manifest, image_file, chip=None, psram=None, board_tag=None,
                 declared=None, mac=None, first_flash=False, manager_overwrite=False,
                 facts_tags=(), tools=None, devreg="~/.devreg/devreg.py", full_erase=False,
                 overwrite=(), facts_chip=None):
        self.name, self.manifest, self.image = name, manifest, image_file
        self.chip, self.psram = chip, psram
        self.board_tag, self.declared = board_tag, declared
        self.mac = norm_mac(mac)
        self.first_flash, self.manager_overwrite = first_flash, manager_overwrite
        self.full_erase = full_erase
        self.overwrite = tuple(overwrite or ())
        self.facts_tags = list(facts_tags or ())
        self.facts_chip = facts_chip or {}      # the survey's esptool probe: partitions, gathered
        self.tools = chipio.tools_of(tools, chip)
        self.devreg = os.path.expanduser(devreg)
        with open(image_file, "rb") as f:
            self.blob = f.read()
        self.regions = regions(self.blob)
        self.chip_facts, self.differ, self.deferred = None, None, None
        self.mode, self.note, self.fresh, self.wrote = None, None, False, None
        self.boots = None

    def _port(self):
        port, why = common.esp_port(self.devreg, self.name, self.tools["esptool"], self.chip)
        if why:
            raise common.port_failure(self.name, why, before_write=self.wrote is None)
        return port

    def _mac_args(self):
        return ["--mac", self.mac] if self.mac else []

    # ---- the four steps ----------------------------------------------------------------------
    def device(self):
        port = self._port()
        with chipio.booted_after(self.tools["esptool"], port, self.chip):
            out = chipio.blocks(self.tools, port, "match", self.image,
                                *(f"{lo:#x}:{hi:#x}" for _n, lo, hi in self.regions),
                                *self._mac_args(), timeout=300)
        self.chip_facts = {k: out.get(k) for k in ("mac", "flash_size", "chip", "size")}
        if self.mac and out.get("mac_ok") is False:
            raise common.FlashError(f"{self.name}: the chip on its port has MAC {out.get('mac')}, "
                                    f"the registry's is {self.mac}: another board is plugged in")
        same = [r["same"] for r in out["regions"]]
        differ = [n for (n, _lo, _hi), s in zip(self.regions, same) if not s]
        self.boot_differs = "boot" in differ
        self.differ = [n for n in differ if n != "boot"]
        self.boots = None
        if "table" not in self.differ:
            self.boots = self._boots(out)
            if self.boots:
                self.differ.append("otadata")
        self.note = ("the chip's bootloader is not the image's (an update keeps the one the "
                     "board has; a full install writes it)" if self.boot_differs else None)
        self.mode = self.deferred = None
        self.fresh = False
        if "table" in self.differ:
            # another firmware's partition table: nothing of its config survives the write
            self.mode, self.fresh = "full", True
            self.deferred = ("the chip's partition table is not the image's: another firmware, "
                             "so the whole chip is written (a first flash)")
        elif self.full_erase:
            self.mode, self.fresh = "full", True
            self.deferred = ("a full install (full_erase): the whole chip is written, every data "
                             "partition as the image has it, so the firmware's config and "
                             "identity are gone")
        elif self.differ:
            self.mode = "update"
            apps = [n for n in self.differ if n != "otadata"]
            what = []
            if apps:
                what.append("the app partitions that differ (" + ", ".join(apps) + ")")
            if self.boots:
                what.append("otadata (the board %s), so it boots the image's slot" % self._boots_text())
            self.deferred = ("an update: only " + " and ".join(what) + " are written; the "
                             "bootloader, the table and the other data partitions (config, "
                             "identity) are kept")
        if self.mode is None:
            return identity.make(self.manifest["family"], hash=self.manifest["hash"])
        apps = [n for n in self.differ if n != "otadata"]
        why = [", ".join(apps) + " differ"] if apps else []
        if self.boots:
            why.append(self._boots_text())
        why = "; ".join(why) or "full erase asked"
        return identity.make(self.manifest["family"], hash=f"other ({why})")

    def _otadata_row(self):
        rows = esppart.parse(self.blob, esppart.OFFSET)
        return rows, next((r for r in rows if r["type"] == 1 and r["subtype"] == 0), None)

    def _boots(self, out):
        """(the slot the chip boots, the image's) when they differ, else None. The chip's table
        is the image's (the caller asks only then), so the image's rows place both."""
        if "otadata" not in out:
            raise common.FlashError(
                f"{self.name}: the bench host's chip_blocks.py does not say which app the chip "
                "boots (its otadata): it is older than this checkout. Bring the host's tools up "
                "to date with `just hosts-setup <layout>`; nothing was written")
        rows, row = self._otadata_row()
        if row is None:
            return None
        mine = self.blob[row["offset"]:row["offset"] + row["size"]] or None
        want = esppart.boot_app(rows, mine)
        have = esppart.boot_app(rows, bytes.fromhex(out["otadata"]) if out["otadata"] else None)
        if want is None or have is None or want["offset"] == have["offset"]:
            return None
        return have["label"] or f"app@{have['offset']:#x}", want["label"] or f"app@{want['offset']:#x}"

    def _boots_text(self):
        return f"boots {self.boots[0]}, not {self.boots[1]}"

    def _update_parts(self):
        """[(name, lo, hi)] an update writes: the image's apps that differ, and its otadata when
        the board boots another slot."""
        out = [r for r in app_regions(self.blob) if r[0] in self.differ]
        if self.boots:
            _rows, row = self._otadata_row()
            out.append(("otadata", row["offset"], row["offset"] + row["size"]))
        return out

    #: the check before the write has a spot form: the table and the overlapped slots alone
    # identity: the whole-image match is the slow read a plan makes (docs/ARCHITECTURE.md#checking-depth)
    spot_reads = ("before write", "identity")

    def depth_moments(self):
        return {"table_change": bool(self.differ and "table" in self.differ),
                "erase": bool(self.full_erase),
                "no_full_record": common.no_full_record(self.facts_chip)}

    def preflight(self):
        out = common.common_refusals(self.manifest, FORMATS, self.board_tag, self.declared)
        if self.manifest.get("hash") != common.sha256_of(self.image):
            out.append("the image file is not the one its manifest describes (SHA-256 differs)")
        out += image_refusals(self.blob, self.chip)
        if not self.mac:
            out.append("the registry holds no MAC for this chip (`policy.chip_mac`, or a "
                       "`usb_serial` that is the MAC): nothing proves which chip would be written")
        size = (self.chip_facts or {}).get("size")
        if size:
            if len(self.blob) > size:
                out.append(f"the image is {len(self.blob)} B, the chip only {size} B")
            far = max((r["offset"] + r["size"] for r in esppart.parse(self.blob, esppart.OFFSET)),
                      default=0)
            if far > size:
                out.append(f"the image's partition table reaches {far:#x}, past the chip's "
                           f"{size >> 20} MB")
        # The write rule over the survey's record of the chip, for what this write would cover
        # (device() chose the mode): the plan's word; write() asks the chip itself.
        if self.mode:
            out += common.recorded_write_refusals(
                self.facts_chip, self.facts_tags, self._writes(),
                common.overwrite_allowed(self.overwrite, self.manager_overwrite),
                "writing the whole chip", new_table=self._new_table())
        if self.boots and self.mode == "update":
            _rows, row = self._otadata_row()
            if row["offset"] + row["size"] > len(self.blob):
                out.append(f"the board {self._boots_text()}, and the image does not carry the "
                           "otadata that would point it back: a full install (full_erase) writes it")
        if self.differ and "table" in self.differ:
            if not self.first_flash:
                out.append("the chip's partition table is another firmware's: writing it is a "
                           "first flash; declare bench_first_flash: true for this board")
            else:
                out += self.first_flash_refusals()
        return out

    def first_flash_used(self):
        return bool(self.differ and "table" in self.differ)

    def first_flash_refusals(self):
        tags = set(self.facts_tags)
        out = []
        missing = [t for t in REQUIRED_TAGS if t not in tags]
        if missing:
            out.append("the survey facts do not show " + ", ".join(missing) + ": run "
                       "`just survey <host> true` for this board before its first flash")
        want = f"psram:{self.psram}" if self.psram else None
        have = sorted(t for t in tags if t.startswith("psram:"))
        if want and have and want not in have:
            out.append(f"the registry says {self.board_tag}, which has {want}, but the chip "
                       f"reports {', '.join(have)}: the wrong board type for this image")
        return out

    def _writes(self):
        """[(offset, length)] this write covers, by its mode: an update the app partitions (and
        the table's run), a full erase the whole chip, else the image's span."""
        return ([(lo, hi - lo) for _n, lo, hi in self._update_parts()]
                + [(lo, hi - lo) for n, lo, hi in self.regions if n == "table"]
                if self.mode == "update" else common.WHOLE_CHIP if self.full_erase
                else [(0, len(self.blob))])

    def _new_table(self):
        return self.blob[esppart.OFFSET:esppart.OFFSET + esppart.LENGTH]

    def write(self):
        self.wrote = self.mode       # verify() reads the board again, and with it the mode
        port = self._port()
        # The chip itself, not the survey's record, is asked once more just before the write.
        why = common.esp_write_refusals(
            self.tools["esptool"], self.chip, port, self._writes(),
            allowed=common.overwrite_allowed(self.overwrite, self.manager_overwrite),
            check_depth=(self.checked or {}).get("depth", "full"), what="writing the whole chip",
            new_table=self._new_table(),
            blocks=functools.partial(chipio.blocks, self.tools))
        if why:
            raise common.FlashError("refused: " + "; ".join(why))
        with chipio.booted_after(self.tools["esptool"], port, self.chip):
            if self.mode == "update":
                runs = ",".join(f"{lo:#x}:{hi:#x}" for n, lo, hi in self.regions if n == "table")
                chipio.blocks(self.tools, port, "region-update", self.image,
                              *(f"{lo:#x}:{hi:#x}" for _n, lo, hi in self._update_parts()),
                              "--runs", runs, *self._mac_args(), timeout=1800)
            else:
                chipio.blocks(self.tools, port, "place", self.image, *self._mac_args(),
                              timeout=1800)

    def verify(self):
        self.full_erase = False      # asked of the write, not of what the board runs after it
        return self.device()
