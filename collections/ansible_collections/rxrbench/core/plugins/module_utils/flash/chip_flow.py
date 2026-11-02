"""One flow per chip verb over every route (docs/ARCHITECTURE.md#chip-and-region-flows):
a whole chip read to a backup, and written back from one. Each route supplies only what differs
(ChipRoute, ChipSession): how the chip is reached, what identifies it, its own meta fields.

A backup is `<dest>/<board>/chip/<stamp>/{meta.json,flash.bin.gz}`, the snapshot (if named) a
link to it under `<dest>/<board>/snapshot/`. A restore goes back only to the board, the route and
the chip it was read from, writes only when the chip differs, and must leave the chip hashing to
the backup. An erase (docs/ARCHITECTURE.md#chip-and-region-flows) blanks the whole chip, only the registry's
chip, read in the same session, only with a backup of that chip on hand unless the caller says
none is wanted, and must leave the chip reading erased. Each flow ends the module run.
"""
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import time

from .common import FlashError


def sha256_file(path, opener=open):
    h = hashlib.sha256()
    with opener(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ChipSession:
    """One route's hold on one chip for one verb (a context). After it is entered: `device`
    (what the board ran, where the route can say; recorded in the backup). After it is left:
    `result` (the module result's fields of the route's own, e.g. `came_back_as`) and `meta`
    (a backup's meta.json fields known only once the board is back)."""

    device = None

    def __init__(self, route, verb):
        self.route, self.verb, self.result, self.meta = route, verb, {}, {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, raw):
        """The whole flash to `raw`. Returns (its size as the chip gives it, the chip's facts for
        meta.json `chip`, the route's own meta.json fields)."""
        raise NotImplementedError

    def refusals(self, meta, size, raw):
        """Why the backup (its meta.json; its image `size` bytes, at `raw`) must not go to this
        chip, read now: another chip, another size."""
        return []

    def differ(self, raw):
        """(whether the chip differs from the image at `raw`, the result's fields: how much)."""
        raise NotImplementedError

    def write(self, raw):
        """The image at `raw` written where the chip differs; the chip must hash to it after.
        Returns the result's fields, `msg` among them."""
        raise NotImplementedError

    def identify(self):
        """(the chip's facts as a backup records them, its whole flash in bytes), read now and
        checked against the registry: another chip is refused (FlashError) before anything is
        erased."""
        raise NotImplementedError

    def erase(self):
        """The whole flash erased; it must read erased after (FlashError otherwise). Returns the
        result's fields, `msg` among them."""
        raise NotImplementedError


class ChipRoute:
    """A core route to a whole chip: one class per core flasher (access.CHIP_ROUTES)."""
    #: recorded in meta.json as `route`; None writes no `route` (the sim's backups never had one)
    name = None
    #: a backup with no `route` in its meta.json is this route's (esptool's before routes were
    #: recorded, the sim's)
    takes_unrouted = False

    def __init__(self, p):
        self.p = p

    def prepare(self, verb):
        """Refuse before anything is touched."""

    def plan_words(self):
        return f"would read {self.p['board']}'s whole flash chip"

    def refusals(self, meta, size):
        """Why the backup must not go to this board, from what the registry says of the chip,
        before the chip is touched (another chip id, another size)."""
        return []

    def session(self, verb):
        raise NotImplementedError

    def same_chip(self, meta, chip):
        """Whether a backup (its meta.json) was read from the chip whose facts are `chip`, as
        read now. By default the route's own refusals of that backup, which compare it with
        the chip the registry names."""
        return not self.refusals(meta, meta.get("size") or 0)


def _stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _snapshot(dest, board, name, stamp):
    snaps = os.path.join(dest, board, "snapshot")
    os.makedirs(snaps, mode=0o700, exist_ok=True)
    link = os.path.join(snaps, name)
    os.symlink(os.path.join("..", "chip", stamp), link + ".new")
    os.replace(link + ".new", link)


def backup(module, p, route):
    """chip_backup: the whole flash read in one session and kept, gzipped, with its record."""
    snapshot = p.get("snapshot")
    if snapshot and not re.fullmatch(r"[A-Za-z0-9._-]+", snapshot):
        raise FlashError(f"a snapshot name is letters, digits, '.', '_' and '-': {snapshot!r}")
    route.prepare("backup")
    if module.check_mode:
        module.exit_json(changed=False, msg=route.plan_words())
    dest = os.path.expanduser(p["dest"])
    stamp = _stamp()
    out = os.path.join(dest, p["board"], "chip", stamp)
    raw = os.path.join(out, "flash.bin")
    s = None
    try:
        with route.session("backup") as s:
            os.makedirs(os.path.dirname(out), mode=0o700, exist_ok=True)
            os.makedirs(out, mode=0o700)
            started = time.monotonic()
            size, chip, extra = s.read(raw)
            took = round(time.monotonic() - started, 1)
        got = os.path.getsize(raw) if os.path.exists(raw) else 0
        if got != size:
            raise FlashError(f"the read gave {got} B, the chip's flash is {size} B")
        digest = sha256_file(raw)
        with open(raw, "rb") as fi, gzip.open(raw + ".gz", "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
        os.remove(raw)
        os.chmod(raw + ".gz", 0o600)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    meta = {"board": p["board"], "taken": stamp, "kind": "chip",
            **({"route": route.name} if route.name else {}), "chip": chip, "size": size,
            "sha256": digest, "file": "flash.bin.gz", "gz_sha256": sha256_file(raw + ".gz"),
            "read_seconds": took, "family": p.get("family"), "device": s.device, **extra,
            **s.meta}
    if snapshot:
        meta["snapshot"] = snapshot
    with open(os.path.join(out, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    if snapshot:
        _snapshot(dest, p["board"], snapshot, stamp)
    module.exit_json(changed=True, path=out, chip=chip, sha256=digest, device=s.device,
                     snapshot=snapshot, read_seconds=took, **s.result)


def refusals(meta, p, route, digest):
    """Why the backup (its meta.json, its image hashing to `digest`) must not go to this board
    by this route, before the chip is read."""
    out = []
    if meta.get("board") != p["board"]:
        out.append(f"the backup is of {meta.get('board')!r}, not {p['board']!r}")
    was = meta.get("route")
    if not (was == route.name or (was is None and route.takes_unrouted)):
        out.append(f"the backup was read by another route ({was or 'esptool'}), not "
                   f"{route.name or 'this one'}")
    if digest != meta.get("sha256"):
        out.append("the backup's image does not hash to what was read")
    return out


def restore(module, p, route, path):
    """chip_restore: the backup written back to the chip it was read from, where the chip
    differs; the chip must hash to it after. A plan says how much differs and writes nothing."""
    try:
        with open(os.path.join(path, "meta.json")) as f:
            meta = json.load(f)
    except (OSError, ValueError) as e:
        raise FlashError(f"{path}: refused: no readable meta.json ({e})") from e
    route.prepare("restore")
    tmpd = tempfile.mkdtemp()
    try:
        raw = os.path.join(tmpd, "flash.bin")
        with gzip.open(os.path.join(path, meta.get("file", "flash.bin.gz")), "rb") as fi, \
                open(raw, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
        digest, size = sha256_file(raw), os.path.getsize(raw)
        why = refusals(meta, p, route, digest) + route.refusals(meta, size)
        if why:
            raise FlashError(f"{path}: refused: " + "; ".join(why))
        result = {"path": path, "taken": meta.get("taken"), "recorded": meta.get("device"),
                  "snapshot": meta.get("snapshot")}
        with route.session("restore") as s:
            why = s.refusals(meta, size, raw)
            if why:
                raise FlashError(f"{path}: refused: " + "; ".join(why))
            differs, how = s.differ(raw)
            result.update(how)
            if module.check_mode or not differs:
                module.exit_json(changed=differs, msg=how.get("msg") or (
                    "the chip holds the backup already" if not differs else "would write it back"),
                    **{k: v for k, v in result.items() if k != "msg"})
            if not p.get("apply_allowed"):
                raise FlashError("refused: the layout does not allow writes (bench_apply_allowed)")
            result.update(s.write(raw))
        result.update(s.result)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    module.exit_json(changed=True, **result)


# ---- the whole chip erased (docs/ARCHITECTURE.md#chip-and-region-flows) --------------------------------------

def this_chips_backups(p, route, chip):
    """The stamps of the chip backups on this host read from this very chip by this route,
    newest first: the board's, the route's (as a restore would take them), and the same chip
    (`route.same_chip`)."""
    root = os.path.join(os.path.expanduser(p.get("dest") or "~/rxrbench/backups"), p["board"],
                        "chip")
    out = []
    for d in sorted(os.listdir(root), reverse=True) if os.path.isdir(root) else []:
        try:
            with open(os.path.join(root, d, "meta.json")) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        was = meta.get("route")
        if meta.get("board") != p["board"] or \
                not (was == route.name or (was is None and route.takes_unrouted)):
            continue
        if route.same_chip(meta, chip):
            out.append(d + (f" (snapshot {meta['snapshot']})" if meta.get("snapshot") else ""))
    return out


def _described(chip):
    keys = ("uid", "chip_id", "mac")
    got = [f"{k} {chip[k]}" for k in keys if chip.get(k)]
    return ", ".join(got) or "unnamed"


def erase(module, p, route):
    """chip_erase: the whole flash erased, after the chip is read and found to be the
    registry's, in the same session. A plan says what would be erased and which backup of
    this chip there is. Refused with no backup of this chip, unless `without_backup`."""
    route.prepare("erase")
    with route.session("erase") as s:
        chip, size = s.identify()
        backups = this_chips_backups(p, route, chip)
        what = f"{p['board']}'s whole flash ({size} B; chip {_described(chip)})"
        kept = (f"newest backup of this chip: {backups[0]}" if backups else
                f"no backup of this chip on its bench host: `just backup <layout> {p['board']}` "
                "takes one first")
        if module.check_mode:
            module.exit_json(changed=True, chip=chip, size=size, backups=backups,
                             msg=f"would erase {what}; {kept}")
        if not p.get("apply_allowed"):
            raise FlashError("refused: the layout does not allow writes (bench_apply_allowed)")
        if not backups and not p.get("without_backup"):
            raise FlashError(f"refused: {what} has no backup of this chip on its bench host, and "
                             f"an erase cannot be undone. Take one first (`just backup <layout> "
                             f"{p['board']}`), or, for a chip whose contents are not wanted, say "
                             "so: -e erase_without_backup=true. Nothing was erased")
        started = time.monotonic()
        result = s.erase()
        result["erase_seconds"] = round(time.monotonic() - started, 1)
    module.exit_json(changed=True, chip=chip, size=size, backups=backups, **s.result, **result)
