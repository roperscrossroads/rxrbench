"""Which route reaches a board's config regions, by core's own flasher names (as common.CAPABILITIES
is keyed), and the three verbs over it: docs/ARCHITECTURE.md#chip-and-region-flows. Each route supplies only
what differs (region_flow.Route); the flow of each verb is region_flow.py's. Each verb ends the
module run. A plugin's flasher has no route here: its family's task files carry the region
verbs.
"""
from . import (esp_image_regions, esptool_chip, esptool_regions, picochip, region_flow, simchip,
               swd_regions, swdchip)
from .common import FlashError
from .region_flow import backup_path, regions_of

ROUTES = {
    # a whole merged image: its own partition table is the map
    "esp-image": esp_image_regions.EspImage,
    # an RP2040 or RP2350 through its ROM bootloader
    "picoboot": picochip.Picoboot,
    # an ESP32 written part by part through its ROM loader
    "esptool": esptool_regions.Esptool,
    # a chip behind a debug probe: the ranges declared for it are the map
    "swd": swd_regions.Swd,
}


def route(p):
    """The route for the board of module params `p`; esptool for any other flasher, as before."""
    return (ROUTES.get(p.get("flasher")) or ROUTES["esptool"])(p)


def _regions(p):
    """The config regions: those declared for the chip (`flash_regions`), else the image's map's."""
    return regions_of(p["image"], declared=p.get("flash_regions"))


def backup(module, p):
    region_flow.backup(module, p, route(p), _regions(p))


def restore(module, p):
    region_flow.restore(module, p, route(p), backup_path(p["dest"], p["board"], p["backup"]),
                        _regions(p))


def erase(module, p):
    """One config region, named: nothing is erased that the caller did not name. With none named
    the refusal lists the board's config regions and points at the whole-chip erase."""
    regions = _regions(p)
    if not p.get("region"):
        listed = ", ".join(f"{n} ({s} B at {o:#x})" for n, o, s in regions)
        raise FlashError(f"name the config region to erase: {p['board']}'s are {listed} "
                         f"(`just erase <layout> {p['board']} <region>`). The whole chip is "
                         f"`just erase-chip`. Nothing was erased")
    return region_flow.erase(module, p, route(p), regions, p["region"])


# ---- the chip level: a whole chip read and written back ---------------------------------------

#: Each module gives backup(module, p) and restore(module, p, path), each ending the module run.
CHIP_ROUTES = {
    "picoboot": picochip,          # an RP2040 or RP2350 through its ROM bootloader
    "swd": swdchip,                # a chip behind a debug probe
    "esptool": esptool_chip,       # an ESP32 through its ROM loader (esp-image too)
}


def _chip_route(p):
    if p.get("run_dir"):   # a simulated board's chip is its flash file, whatever flashes it
        return simchip
    return CHIP_ROUTES.get(p.get("flasher")) or CHIP_ROUTES["esptool"]


def chip_backup(module, p):
    _chip_route(p).backup(module, p)


def chip_restore(module, p, path):
    _chip_route(p).restore(module, p, path)


def chip_erase(module, p):
    _chip_route(p).erase(module, p)
