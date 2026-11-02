"""The SWD route to a chip's config regions: a chip behind a debug probe, its config regions the
ranges declared for it with `role: config` (docs/ARCHITECTURE.md#partitions-and-the-write-rule,
docs/ARCHITECTURE.md#chip-and-region-flows).

A declared range belongs to the image that declares it, so the session first shows the chip
holds that image, as the SWD flasher shows what a chip runs: the hash of the flash over the
image's length, at the image's base. Each read, write and erase is one host-tool session,
guarded by the chip's unique id (swd.Probe); a backup records that id, and a restore refuses a
backup read from another chip. A write is checked by the chip's own hash of the range after it,
and a region that already holds the backup's bytes is not written.
"""
import hashlib
import os

from .. import identity
from . import common, region_flow, swd, swdchip


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class _Session(region_flow.Session):
    """No console and no bootloader to enter: the probe holds the chip only for each call. On
    the way in, the chip must hold the image whose ranges apply."""

    def __enter__(self):
        r = self.route
        sha = r.probe.call("sha", str(r.length), at=r.base)["sha256"]
        self.device = identity.make(r.manifest["family"], hash=sha)
        self.tag = sha[:8]
        if sha != r.image_hash and not (self.verb == "erase" and r.p.get("wedged")):
            raise common.FlashError(
                "the chip does not hold this image (its flash over the image's length hashes to "
                f"{sha[:12]}, the image to {r.image_hash[:12]}), so the ranges the image declares "
                "may not be the chip's"
                + ("; erasing it anyway needs `wedged: true`, declared" if self.verb == "erase" else ""))
        return self

    def read(self, regions, out):
        for name, off, size in regions:
            self.route.probe.call("read", os.path.join(out, f"{name}.bin"), str(size),
                                  timeout=600, at=off)

    def _holds(self, part):
        got = self.route.probe.call("sha", str(part["size"]), timeout=600, at=part["offset"])
        return got["sha256"] == part["sha256"]

    def plan(self, parts):
        todo = [x for x in parts if not self._holds(x)]
        return todo, "would write " + ", ".join(
            f"{x['name']} ({x['size']} B at {x['offset']:#x})" for x in todo)

    def write(self, parts, meta):
        probe = self.route.probe
        why = region_flow.chip_refusals(meta, {"uid": probe.uid})
        if why:
            raise common.FlashError("refused: " + "; ".join(why))
        for x in parts:
            if _sha256(x["file"]) != x["sha256"]:
                raise common.FlashError(f"{x['name']}: the backup's file is not what was read")
            probe.call("write", x["file"], timeout=900, at=x["offset"])
            if not self._holds(x):
                raise common.FlashError(f"after the write, {x['name']} does not hash to the backup")

    def erase(self, name, offset, size):
        self.route.probe.call("erase", str(size), timeout=600, at=offset)


class Swd(region_flow.Route):
    """A chip behind a debug probe: its declared config ranges are the map."""
    name = "swd"

    def prepare(self, verb):
        self.probe = swdchip.probe_of(self.p)
        self.manifest, image_file = common.load_image(self.p["image"])
        if self.manifest["format"] not in swd.FORMATS:
            raise common.FlashError(f"the SWD route reads a chip's ranges for a raw image; this "
                                    f"image is {self.manifest['format']}")
        with open(image_file, "rb") as f:
            data = f.read()
        self.length, self.image_hash = len(data), hashlib.sha256(data).hexdigest()
        base = self.manifest.get("base")
        self.base = int(str(base), 0) if base is not None else None

    def chip(self):
        return {"uid": ((self.p.get("route") or {}).get("uid") or "").lower() or None}

    def session(self, verb, regions, name=None):
        return _Session(self, verb)
