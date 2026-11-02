"""The esptool route to a board's config regions: an ESP32 written part by part through its ROM
loader, the config regions where the image's flash map puts them (docs/ARCHITECTURE.md#chip-and-region-flows).

The board's console shows what it runs before anything is touched: the image's map is the
board's only when it runs the image. Then its broker lets go of the port, and the regions are
read, written or erased through the loader; the board must come back on its console after.

The chip is the loader's to name: esptool prints its MAC on every connect. A backup records
it (refused when the registry holds another MAC: another board is on the port), and a restore
reads it before the write and refuses a backup read from another chip.
"""
import os
import re

from .. import broker_client, identity, stateline
from . import chip, common, espimage, region_flow
from .region_flow import erase_refusals

#: the MAC esptool prints on connect ("MAC: aa:bb:..."; esptool 5 aligns it in columns)
_MAC = re.compile(r"^\s*(?:BASE )?MAC:\s*((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\b", re.I | re.M)


def mac_of(text):
    m = _MAC.search(text or "")
    return m.group(1).lower() if m else None


def _not_this_image(device, manifest):
    return common.FlashError(
        "the board does not run this image, so this image's flash map may not be the board's: "
        + (", ".join(f"{k} is {a!r}, the image has {v!r}" for k, (a, v) in
                     identity.differences(device, common.image_identity(manifest)).items())
           or "it cannot be told apart"))


class _Session(region_flow.Session):
    """The board's console held for the verb: read on the way in, released to the loader on
    the first chip access, and waited for on the way out."""

    def __enter__(self):
        r, p = self.route, self.route.p
        self.released, self.answered = False, True
        self.board = broker_client.Board(p["board"], path=p["socket"], timeout=p["settle"] + 120)
        self.board.__enter__()
        try:
            if self.verb == "erase":
                try:
                    device = common.read_board(self.board, r.manifest["family"], deadline=5.0)
                    runs = identity.same(device, common.image_identity(r.manifest)) is True
                except (common.FlashError, broker_client.BrokerError, stateline.StateError):
                    device, self.answered, runs = None, False, False
                why = erase_refusals(r.manifest, self.regions, self.name, self.answered, runs,
                                     p["wedged"])
                if why:
                    raise common.FlashError("refused: " + "; ".join(why))
            else:
                device = common.read_board(self.board, r.manifest["family"])
                if identity.same(device, common.image_identity(r.manifest)) is not True:
                    raise _not_this_image(device, r.manifest)
        except BaseException:
            self.board.__exit__(None, None, None)
            raise
        self.device = device
        self.tag = (device or {}).get("build", "")
        return self

    def __exit__(self, exc_type, *_rest):
        r, p = self.route, self.route.p
        try:
            if self.released:
                try:
                    after = common.wait_back(self.board, r.manifest["family"], p["settle"])
                except common.FlashError:
                    # a board erased because it did not answer need not answer after
                    if self.answered:
                        raise
                else:
                    self.result["came_back_as"] = after
                    if (exc_type is None and self.verb == "restore"
                            and identity.same(after, self.device) is not True):
                        raise common.FlashError(f"the board came back as {after}, "
                                                f"not {self.device}")
        finally:
            self.board.__exit__(None, None, None)
        return False

    def _loader(self):
        """The board's port, its broker let go of it."""
        r, p = self.route, self.route.p
        if not self.released:
            self.board.release(ttl=p["settle"] + 300)
            self.released = True
        port, why = common.esp_port(r.devreg, p["board"], r.esptool, r.mcu)
        if why:
            raise common.port_failure(p["board"], why)
        return port

    def _is_the_registrys(self, mac):
        """Refuse a chip that is not the one the registry names (another board on the port)."""
        want = self.route.chip()["mac"]
        if want and mac != want:
            raise common.FlashError(f"refused: the chip on the board's port has MAC {mac}, the "
                                    f"registry's is {want}: another board")

    def _write_rule(self, port, ranges, what):
        """The write rule (docs/ARCHITECTURE.md#partitions-and-the-write-rule): on a chip with an app manager every data
        region is shared by its apps, and an app slot may hold other firmware."""
        r, p = self.route, self.route.p
        why = common.esp_write_refusals(
            r.esptool, r.mcu, port, ranges,
            allowed=common.overwrite_allowed(p["overwrite"], p["app_manager_overwrite"]),
            what=what, blocks=chip.blocks_if_installed(chip.tools_of(p["tools"], r.mcu)))
        if why:
            raise common.FlashError("refused: " + "; ".join(why))

    def read(self, regions, out):
        r = self.route
        port = self._loader()
        # One loader session for every region: the board does not boot between two reads,
        # so it cannot write its config between them.
        with chip.booted_after(r.esptool, port, r.mcu):
            for i, (name, off, size) in enumerate(regions):
                path = os.path.join(out, f"{name}.bin")
                before = "default-reset" if i == 0 else "no-reset"
                rc, text = common.tool([r.esptool, "--chip", r.mcu, "--port", port, "--baud",
                                        "460800", "--before", before, "--after", "no-reset",
                                        "read-flash", f"{off:#x}", f"{size:#x}", path],
                                       timeout=240)
                if rc != 0 or not os.path.exists(path) or os.path.getsize(path) != size:
                    raise common.FlashError(f"reading {name} failed (rc {rc}): "
                                            + text.strip()[-300:])
                if i == 0:
                    mac = mac_of(text)
                    if not mac:
                        raise common.FlashError("esptool did not print the chip's MAC, and a "
                                                "backup is tied to its chip: nothing was kept")
                    self._is_the_registrys(mac)
                    self.chip = {"mac": mac}

    def write(self, parts, meta):
        r = self.route
        port = self._loader()
        rc, text = common.tool([r.esptool, "--chip", r.mcu, "--port", port, "read-mac"],
                               timeout=120)
        mac = mac_of(text)
        if rc != 0 or not mac:
            raise common.FlashError(f"reading the chip's MAC failed (rc {rc}), and a backup goes "
                                    f"back only to its chip: " + text.strip()[-300:])
        self._is_the_registrys(mac)
        why = region_flow.chip_refusals(meta, {"mac": mac})
        if why:
            raise common.FlashError("refused: " + "; ".join(why))
        self._write_rule(port, [(x["offset"], x["size"]) for x in parts],
                         "writing the config regions")
        argv = [r.esptool, "--chip", r.mcu, "--port", port, "--baud", "460800", "write_flash",
                "--flash_mode", "keep", "--flash_freq", "keep", "--flash_size", "keep"]
        for x in parts:
            argv += [f"{x['offset']:#x}", x["file"]]
        rc, text = common.tool(argv, timeout=300)
        if rc != 0 or text.count("Hash of data verified") != len(parts):
            raise common.FlashError(f"esptool failed (rc {rc}): " + text.strip()[-400:])

    def erase(self, name, offset, size):
        r = self.route
        port = self._loader()
        # The chip's own table first: an app manager's slots and data lie where this image's
        # map says a config region does.
        self._write_rule(port, [(offset, size)], f"erasing {name}")
        rc, text = common.tool([r.esptool, "--chip", r.mcu, "--port", port, "erase_region",
                                f"{offset:#x}", f"{size:#x}"], timeout=240)
        if rc != 0:
            raise common.FlashError(f"esptool erase_region failed (rc {rc}): "
                                    + text.strip()[-300:])


class Esptool(region_flow.Route):
    """An ESP32 written part by part: the image's flash.json is the map."""
    name = "esptool"
    plan_reads = True   # the console shows what the board runs, also for a plan

    def prepare(self, verb):
        p = self.p
        self.esptool = os.path.expanduser(p["tools"].get("esptool") or "~/.venv-esp/bin/esptool")
        self.devreg = os.path.expanduser(p["tools"].get("devreg") or "~/.devreg/devreg.py")
        self.mcu = p["chip"]
        if not self.mcu:
            raise common.FlashError("the board's chip is not known: its type (the registry's "
                                    "board_tag) is not in catalog/boards.yml, so esptool cannot "
                                    "be told what it is")
        self.manifest, _file = common.load_image(p["image"])
        if verb != "erase" and self.manifest["format"] != "esp-parts":
            raise common.FlashError(f"{verb} reads and writes ESP32 flash part by part; this "
                                    f"image is {self.manifest['format']}")

    def chip(self):
        return {"mac": espimage.norm_mac(self.p.get("mac"))}

    def session(self, verb, regions, name=None):
        s = _Session(self, verb)
        s.regions, s.name = regions, name
        return s
