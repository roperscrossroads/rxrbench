"""The region level of a board written with a whole merged ESP32 image (the `esp-image`
flasher, espimage.py): the data partitions its family names (config_regions: a stock
release's file system, which holds its prefs and identity key, and its NVS), read, written back
and erased through the ROM loader. Shared by region_backup, region_restore and region_erase.

Such a board may have no console this tool speaks, so nothing is asked of its firmware. Where
its config regions are is the image's own partition table, and that it is the board's is proven the
way the flasher proves what a board runs: the loader's stub hashes the chip over the image's
table and apps, in the same session as the read, write or erase (`chip_blocks.py
region-*`), on the chip whose MAC the registry holds. A restore needs the whole image to match
(it writes the config back under a known image). A backup and an erase need only the table, which
is what says where the config regions are: a board one release behind its layout can be backed up
before its upgrade, and a board whose app no longer starts can still be erased (what `wedged`
is for on a board with a console). A backup records that only the table was compared.

The board does not boot between two regions: they are read, written or erased in one session,
then a fresh esptool session boots it (chip.py).
"""
import os

from .. import broker_client
from . import chip as chipio
from . import common, espimage, region_flow


def _image(p):
    if not p.get("chip"):
        raise common.FlashError("the board's chip is not known: its type (the registry's "
                                "board_tag) is not in catalog/boards.yml")
    manifest, image_file = common.load_image(p["image"])
    if manifest.get("format") not in espimage.FORMATS:
        raise common.FlashError(f"an esp-image board's config regions are found through its merged "
                                f"image's partition table; this image is {manifest.get('format')}")
    with open(image_file, "rb") as f:
        blob = f.read()
    return manifest, image_file, blob


def _runs(blob, whole):
    """The regions that must hash as the image's: its identity (the table and the apps; a board
    updated the vendor's way keeps an older bootloader), or the table alone."""
    regions = [r for r in espimage.identity_regions(blob) if whole or r[0] == "table"]
    return ",".join(f"{lo:#x}:{hi:#x}" for _n, lo, hi in regions)


def _session(p, cmd, image_file, parts, runs):
    """One chip_blocks region session on the board's port, the broker (when it has one)
    holding off meanwhile, the board booted after."""
    mac = espimage.norm_mac(p.get("mac"))
    if not mac:
        raise common.FlashError("refused: the registry holds no MAC for this chip "
                                "(`policy.chip_mac`, or a `usb_serial` that is the MAC): nothing "
                                "proves which chip it is")
    tools = chipio.tools_of(p.get("tools"), p["chip"])
    devreg = os.path.expanduser((p.get("tools") or {}).get("devreg") or "~/.devreg/devreg.py")
    with broker_client.board_or_none(p["board"], path=p.get("socket"),
                                     timeout=p.get("settle", 60.0) + 120) as b:
        b.release(ttl=p.get("settle", 60.0) + 600)
        port, why = common.esp_port(devreg, p["board"], tools["esptool"], p["chip"])
        if why:
            raise common.port_failure(p["board"], why)
        with chipio.booted_after(tools["esptool"], port, p["chip"]):
            return chipio.blocks(tools, port, cmd, image_file, *parts, "--runs", runs,
                                 "--mac", mac, timeout=900)


def _match(p, image_file, blob, whole):
    """For a plan: the board's chip against the image (nothing written), and the reason it
    would be refused, or None."""
    mac = espimage.norm_mac(p.get("mac"))
    if not mac:
        return ("the registry holds no MAC for this chip (`policy.chip_mac`, or a `usb_serial` "
                "that is the MAC): nothing proves which chip it is")
    tools = chipio.tools_of(p.get("tools"), p["chip"])
    devreg = os.path.expanduser((p.get("tools") or {}).get("devreg") or "~/.devreg/devreg.py")
    runs = _runs(blob, whole).split(",")
    with broker_client.board_or_none(p["board"], path=p.get("socket"),
                                     timeout=p.get("settle", 60.0) + 120) as b:
        b.release(ttl=p.get("settle", 60.0) + 300)
        port, why = common.esp_port(devreg, p["board"], tools["esptool"], p["chip"])
        if why:
            raise common.port_failure(p["board"], why)
        with chipio.booted_after(tools["esptool"], port, p["chip"]):
            out = chipio.blocks(tools, port, "match", image_file, *runs, "--mac", mac, timeout=300)
    if out.get("mac_ok") is False:
        return f"the chip on its port has MAC {out.get('mac')}, the registry's is {mac}: another board"
    if not all(r["same"] for r in out["regions"]):
        return ("the chip does not run this image" if whole else
                "the chip's partition table is not this image's") + \
            ", so this image's map may not be the board's"
    return None


class _Session(region_flow.Session):
    """No session is held between calls: each chip_blocks call proves the chip (its MAC) and
    what it runs (the hashes in `--runs`) in the same loader session as its read, write or
    erase."""

    def __init__(self, route, verb):
        super().__init__(route, verb)
        r = route
        self.tag = r.manifest["hash"][:8]
        # Only the table is compared for a backup: the board ran an image with this one's
        # table, not necessarily this image, so the record names the image whose table matched.
        self.device = {"family": r.manifest["family"]}
        self.extra = {"matched": "table",
                      "table_of": {"image": os.path.basename(os.path.normpath(r.p["image"])),
                                   "hash": r.manifest["hash"],
                                   "version": r.manifest.get("version")}}

    def check(self):
        # a restore needs the whole image; a backup and an erase the table alone
        why = _match(self.route.p, self.route.image_file, self.route.blob,
                     whole=self.verb == "restore")
        if why:
            raise common.FlashError("refused: " + why)

    def read(self, regions, out):
        r = self.route
        _session(r.p, "region-read", r.image_file,
                 [f"{off:#x}:{off + size:#x}={os.path.join(out, name + '.bin')}"
                  for name, off, size in regions], _runs(r.blob, False))

    def write(self, parts, meta):
        r = self.route
        _session(r.p, "region-write", r.image_file,
                 [f"{x['offset']:#x}:{x['offset'] + x['size']:#x}={x['file']}" for x in parts],
                 _runs(r.blob, True))

    def erase(self, name, offset, size):
        r = self.route
        _session(r.p, "region-erase", r.image_file, [f"{offset:#x}:{offset + size:#x}"],
                 _runs(r.blob, False))


class EspImage(region_flow.Route):
    """A board written with a whole merged image: its own partition table is the map."""
    name = "esp-image"

    def prepare(self, verb):
        self.manifest, self.image_file, self.blob = _image(self.p)

    def chip(self):
        return {"mac": espimage.norm_mac(self.p.get("mac"))}

    def session(self, verb, regions, name=None):
        return _Session(self, verb)
