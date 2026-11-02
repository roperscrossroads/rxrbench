"""A board's config regions: where the build puts them, whether a backup of them may be
written back, and the one flow per verb that backs them up, restores and erases them over any
route (docs/ARCHITECTURE.md#chip-and-region-flows). Shared by region_backup, region_restore and
region_erase."""
import hashlib
import json
import os
import re
import shutil
import time

from .. import esppart, familyspec, regions as declared_ranges
from .common import FlashError

# flash.json's map keys for the regions that hold a node's config are the family's
# (familyspec.py `regions`); the first is the one every backup must hold.


def family_of(image_dir):
    """The family an image directory's manifest names, or None."""
    try:
        with open(os.path.join(image_dir, "manifest.json")) as f:
            return json.load(f).get("family")
    except (OSError, ValueError):
        return None


#: the config regions of the ranges declared for a chip (`bench_flash_regions`, the
#: ranges of a chip with no table, docs/ARCHITECTURE.md#partitions-and-the-write-rule), at
#: absolute addresses
declared_regions = declared_ranges.config_ranges


def regions_of(image_dir, family=None, declared=None):
    """[(name, offset, size)] of the config regions: the declared ranges' config regions when
    there are any, else the family's, from the image's flash map. Raises FlashError if the map
    does not say where the config regions are."""
    out = declared_regions(declared)
    if out:
        return out
    family = family or family_of(image_dir)
    regions = familyspec.of(family)["regions"] if family else None
    if regions is None:
        raise FlashError(f"no config regions are known for {family or 'an unnamed'} firmware"
                         f"{familyspec.unloaded(family)}")
    try:
        fmap = merged_map(image_dir)
        if fmap is None:
            with open(os.path.join(image_dir, "flash.json")) as f:
                fmap = json.load(f)["map"]
        out = [(name, int(fmap[key], 16), int(fmap[key + "_SIZE"], 16))
               for name, key in regions if key in fmap]
    except (OSError, ValueError, KeyError) as e:
        raise FlashError(f"the image's flash.json does not say where the config "
                                f"are ({e}): nothing is read from a guessed offset") from e
    if not out or out[0][0] != regions[0][0] or any(size <= 0 for _n, _o, size in out):
        raise FlashError(f"the image's flash map has no {regions[0][1]} partition")
    return out


def merged_map(image_dir):
    """The flash map of a merged ESP32 image (a release's `.factory.bin`, format `raw-bin`):
    its own partition table at 0x8000, each data partition as `<LABEL>` and `<LABEL>_SIZE`
    (`spiffs` is `SPIFFS`). An app partition is never in it, so never a config region. None
    for an image of another format, or one with no table (then flash.json is the map)."""
    try:
        with open(os.path.join(image_dir, "manifest.json")) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return None
    if manifest.get("format") != "raw-bin" or not manifest.get("file"):
        return None
    with open(os.path.join(image_dir, manifest["file"]), "rb") as f:
        f.seek(esppart.OFFSET)
        rows = esppart.parse(f.read(esppart.LENGTH))
    if not rows:
        return None
    out = {}
    for r in rows:
        if r["type"] == esppart.DATA and r["label"]:
            key = r["label"].upper()
            out[key], out[key + "_SIZE"] = f"{r['offset']:#x}", f"{r['size']:#x}"
    return out


def latest(dest, board):
    """The newest backup directory of `board` under `dest` (names start with a UTC stamp)."""
    root = os.path.join(os.path.expanduser(dest), board)
    try:
        found = sorted(d for d in os.listdir(root)
                       if os.path.isfile(os.path.join(root, d, "meta.json")))
    except OSError:
        found = []
    if not found:
        raise FlashError(f"no backup of {board} under {root}")
    return os.path.join(root, found[-1])


def backup_path(dest, board, backup):
    """Where `backup` is: `latest`, a bare name (one of `board`'s backups under `dest`), or a
    path."""
    if backup == "latest":
        return latest(dest, board)
    if os.sep not in backup:
        return os.path.join(os.path.expanduser(dest), board, backup)
    return os.path.expanduser(backup)


def check_backup(path, board, regions):
    """(meta, refusals) for writing the backup at `path` to `board`, whose running image puts
    the config regions at `regions` [(name, offset, size)]. Any refusal stops the restore."""
    try:
        with open(os.path.join(path, "meta.json")) as f:
            meta = json.load(f)
    except (OSError, ValueError) as e:
        return {}, [f"{path}: no readable meta.json ({e})"]
    out = []
    if meta.get("board") != board:
        out.append(f"the backup is of {meta.get('board')!r}, not {board!r}: its identity key "
                   f"would give two nodes one id")
    have = {r.get("name"): r for r in meta.get("regions") or []}
    if not have:
        out.append("the backup records no regions")
    if regions and regions[0][0] not in have:
        out.append(f"the backup has no {regions[0][0]} partition")
    where = {name: (off, size) for name, off, size in regions}
    for name, r in have.items():
        try:
            off, size = int(r["offset"], 16), int(r["size"])
        except (KeyError, TypeError, ValueError):
            out.append(f"{name}: the backup does not say where it was read")
            continue
        if name not in where:
            out.append(f"{name}: the board's image has no such region")
        elif where[name] != (off, size):
            out.append(f"{name}: read at {off:#x} ({size} B), but the board's image puts it at "
                       f"{where[name][0]:#x} ({where[name][1]} B)")
        file = os.path.join(path, os.path.basename(r.get("file") or ""))
        try:
            with open(file, "rb") as f:
                data = f.read()
        except OSError as e:
            out.append(f"{name}: {e}")
            continue
        if len(data) != size:
            out.append(f"{name}: the file is {len(data)} B, the backup read {size} B")
        elif hashlib.sha256(data).hexdigest() != r.get("sha256"):
            out.append(f"{name}: the file's SHA-256 is not the one recorded when it was read")
    missing = [n for n in where if n not in have]
    if missing and not out:
        # A region the board has and the backup lacks is left as it is; say so in the result.
        meta["left_alone"] = missing
    return meta, out


def erase_refusals(manifest, regions, region, answered, runs_image, wedged):
    """Every reason not to erase `region` of a board ((name, offset, size) list `regions` from
    the image's map). `answered`: the board's firmware answered; `runs_image`: and it runs
    this image (so the map is the board's); `wedged`: the caller declared it does not answer."""
    out = []
    if manifest.get("format") != "esp-parts":
        out.append(f"erasing a region is for ESP32 boards (esptool); this image is "
                   f"{manifest.get('format')}: an nRF52840 behind a UF2 bootloader has no USB erase path (SWD, or the "
                   f"firmware's own wipe)")
    names = [n for n, _o, _s in regions]
    if region not in names:
        out.append(f"{region!r} is not one of this firmware's config regions ({names}): "
                   f"an app slot or a bootloader is never erased here")
    if answered and not runs_image:
        out.append("the board answers but does not run this image, so this image's map may not "
                   "be the board's")
    if not answered and not wedged:
        out.append("the board does not answer; erasing a board that cannot say what it runs "
                   "needs `wedged: true`, declared")
    return out


# ---- the route interface and the one flow per verb --------------------------------------------

class Session:
    """One route's reach into one board for one verb (a context: the console released or the
    bootloader entered on the way in, the board back in its app on the way out). Entering it
    shows the board runs the image whose map says where the config regions are, where the route can
    tell before touching the flash; a route that proves it in the same loader call as the read
    or write (esp-image) proves it there.

    After it is entered: `tag` (the backup directory's suffix), `device` (what the board
    runs, as recorded), `chip` (the chip's identity: {"mac": ...} or {"chip_id": ...}), `extra`
    (meta.json fields of the route's own). After it is left: `result` (the module result's
    fields of the route's own)."""

    tag, device = "", None

    def __init__(self, route, verb):
        self.route, self.verb = route, verb
        self.chip, self.extra, self.result = dict(route.chip()), {}, {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def check(self):
        """For a plan: the proof the route makes before a write, with nothing written."""

    def read(self, regions, out):
        """Each (name, offset, size) of `regions` to <out>/<name>.bin."""
        raise NotImplementedError

    def plan(self, parts):
        """(the parts that need writing, the plan's words). Every part, unless the route can
        tell which already hold the backup's bytes."""
        return parts, "would write " + ", ".join(
            f"{x['name']} ({x['size']} B at {x['offset']:#x})" for x in parts)

    def write(self, parts, meta):
        """Each part's file to its offset, checked by the route's own read or hash. `meta`: the
        backup's record, for a route that reads the chip only now (chip_refusals)."""
        raise NotImplementedError

    def erase(self, name, offset, size):
        raise NotImplementedError


class Route:
    """A core route to a board's flash: one class per core flasher (access.ROUTES)."""
    name = None
    #: a plan of a backup reaches the board (it reads the console to show what it runs)
    plan_reads = False

    def __init__(self, p):
        self.p = p

    def prepare(self, verb):
        """Refuse before anything is touched: the chip is not known, the image is of the wrong
        format."""

    def chip(self):
        """The chip's identity as the registry gives it, before anything is touched (a value
        may be None: then the route reads it from the chip)."""
        return {}

    def session(self, verb, regions, name=None):
        raise NotImplementedError


def _stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _digits(value):
    return re.sub(r"[^0-9a-f]", "", str(value).lower())


def chip_refusals(meta, have):
    """Why a backup (its meta.json) must not go to the chip identified by `have`. A backup made
    by a route records its chip, and goes back only to that chip. A backup from before routes
    recorded their chips (no `route` in its meta) is tied to its board's name alone, which
    check_backup has compared."""
    out = []
    for key, value in have.items():
        if not value:
            continue
        if meta.get(key):
            if _digits(meta[key]) != _digits(value):
                out.append(f"the backup was read from chip {meta[key]}, this board is chip {value}")
        elif meta.get("route"):
            out.append(f"the backup records no chip ({key}), so nothing shows it was read from "
                       f"this one")
    return out


def _records(out, regions):
    read = []
    for name, off, size in regions:
        path = os.path.join(out, f"{name}.bin")
        got = os.path.getsize(path) if os.path.exists(path) else 0
        if got != size:
            raise FlashError(f"reading {name}: {got} B, the map says {size}")
        os.chmod(path, 0o600)
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        read.append({"name": name, "offset": f"{off:#x}", "size": size, "sha256": digest,
                     "file": f"{name}.bin"})
    return read


def _apply_allowed(p):
    if not p.get("apply_allowed"):
        raise FlashError("refused: the layout does not allow writes (bench_apply_allowed)")


def backup(module, p, route, regions):
    """region_backup: every region read in one session,
    `<dest>/<board>/<stamp>-<tag>/{meta.json,<region>.bin}`, the chip recorded."""
    route.prepare("backup")
    plan = "would read " + ", ".join(f"{n} ({size} B at {off:#x})" for n, off, size in regions)
    if module.check_mode and not route.plan_reads:
        module.exit_json(changed=False, msg=plan)
    with route.session("backup", regions) as s:
        if module.check_mode:
            module.exit_json(changed=False, msg=plan)
        stamp = _stamp()
        out = os.path.join(os.path.expanduser(p["dest"]), p["board"], f"{stamp}-{s.tag}")
        os.makedirs(out, mode=0o700)
        try:
            s.read(regions, out)
        except BaseException:
            shutil.rmtree(out, ignore_errors=True)
            raise
    read = _records(out, regions)
    meta = {"board": p["board"], "taken": stamp, "route": route.name, **s.chip,
            "device": s.device, **s.extra, "regions": read,
            "image": os.path.basename(os.path.normpath(p["image"])), **s.result}
    with open(os.path.join(out, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    module.exit_json(changed=True, path=out, regions=read)


def restore(module, p, route, path, regions):
    """region_restore: the backup's regions written back to the chip they were read from, on
    a board that runs the image whose map puts them where they were read."""
    meta, refusals = check_backup(path, p["board"], regions)
    if meta:
        refusals += chip_refusals(meta, route.chip())
    if refusals:
        raise FlashError(f"{path}: refused: " + "; ".join(refusals))
    route.prepare("restore")
    result = {"path": path, "taken_by": meta.get("device"), "table_of": meta.get("table_of"),
              "regions": [{k: r[k] for k in ("name", "offset", "size", "sha256")}
                          for r in meta["regions"]],
              "left_alone": meta.get("left_alone", [])}
    if not meta.get("route"):
        result["chip_checked"] = False
    parts = [{"name": r["name"], "offset": int(str(r["offset"]), 16), "size": int(r["size"]),
              "file": os.path.join(path, os.path.basename(r["file"])), "sha256": r["sha256"]}
             for r in meta["regions"]]
    with route.session("restore", regions) as s:
        todo, words = s.plan(parts)
        if module.check_mode or not todo:
            if todo:
                s.check()
            module.exit_json(changed=bool(todo), msg=(
                words if todo else "the board holds the backup's regions already"), **result)
        _apply_allowed(p)
        s.write(todo, meta)
    result.update(s.result)
    module.exit_json(changed=True, **result)


def erase(module, p, route, regions, name):
    """region_erase: one config region, never an app slot or a bootloader."""
    names = [n for n, _o, _s in regions]
    if name not in names:
        raise FlashError(f"refused: {name!r} is not one of this firmware's config regions "
                         f"({names}): an app slot or a bootloader is never erased here")
    route.prepare("erase")
    _n, off, size = next(r for r in regions if r[0] == name)
    region = {"name": name, "offset": f"{off:#x}", "size": size}
    with route.session("erase", regions, name) as s:
        if module.check_mode:
            s.check()
            module.exit_json(changed=True, region=region)
        _apply_allowed(p)
        s.erase(name, off, size)
    module.exit_json(changed=True, region=region,
                     msg=f"{name} erased: the firmware starts it afresh at its next boot")
