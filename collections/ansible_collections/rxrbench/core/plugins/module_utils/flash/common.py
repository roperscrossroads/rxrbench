"""Putting an image on a device: the four steps every flasher shares.

    inspect    what the image is            (its manifest -> an identity)
    preflight  every reason not to write    (a list of refusals; any refusal stops it)
    write      the flasher's own business
    verify     what the device runs now     (an identity, read from the device)

A flasher is an object with `device()`, `preflight()`, `write()` and `verify()`. `drive()`
runs them, and decides from identities alone: if the device already runs the image nothing
is written; after a write the device must run the image, or the flash has failed whatever
the flasher's tool printed.

An image is a directory holding the file and a `manifest.json` that says what it is:

    {"family": "<family>", "format": "sim-exe", "file": "zephyr.exe",
     "target": "native_sim", "board_tag": "native-sim", "class": 0, "build": "3f3de48a"}

`build` is the id the firmware will report for itself. It is in the manifest because a board
read over its shell reports a build id and no hash, and a version alone proves nothing.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

from .. import broker_client, depth, esppart, familyspec, identity, lastread, regions, stateline, timing

FORMATS = ("sim-exe", "mcuboot-bin", "uf2", "esp-parts", "wic", "raw-bin")

#: Core's own flashers: the names a catalogued image may give in `flasher` without a plugin.
#: A plugin's images may name others, which the plugin declares (its FLASHERS, docs/PLUGINS.md).
FLASHERS = ("sim", "esptool", "uf2", "smp", "swd", "picoboot", "esp-image")
#: The bearers each of core's flashers writes over (routes.py): a board whose flash route is a
#: bearer its image's flasher does not use cannot be written that way.
BEARERS = {"sim": ("sim",), "esptool": ("usb",), "uf2": ("usb",), "smp": ("usb", "ble", "eth", "wifi"),
           "swd": ("swd",), "picoboot": ("usb",), "esp-image": ("usb",)}

#: What a core flasher hands a route that carries the image itself, in place of writing it: the
#: format of the file it would have written (flash/delivery.py). A device whose image names
#: such a flasher can take a delivering route that carries that format, whatever its family.
DELIVERS = {"smp": "mcuboot-bin"}

#: What a flasher can do beside writing an image, so a play asks for the ability and never
#: names a flasher: `regions` (a device's config regions read, written back and erased),
#: `chip` (the whole chip read and written back). A flasher not listed can do neither.
CAPABILITIES = {"esptool": ("regions", "chip"),
                # a simulated board's chip is its flash file (flash/simchip.py): read, written
                # back and erased whole; it has no config regions of its own to name
                "sim": ("chip",),
                # an RP2040's or RP2350's whole flash through its ROM bootloader, and the
                # config regions an image's flash map names (picochip.py)
                "picoboot": ("regions", "chip"),
                # a whole ESP32 image written through the ROM loader (espimage.py): the chip is
                # read and written back whole like any ESP32, and the config regions its family
                # names in the image's own partition table (esp_image_regions.py)
                "esp-image": ("regions", "chip"),
                # a chip behind a debug probe: its whole flash, read and written back through
                # the probe (swdchip.py), and the config regions declared for it (swd_regions.py)
                "swd": ("regions", "chip")}


def can(flasher):
    """What a device flashed this way can have done to it (CAPABILITIES)."""
    return list(CAPABILITIES.get(flasher) or ())


class FlashError(Exception):
    pass


def sha256_of(path):
    """The hash of a file (an image, a chip's flash read out, a sim board's executable)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_image(image_dir, formats=()):
    """(manifest, absolute path of the image file). Raises FlashError if it is not an image.
    `formats`: those a plugin's module writes, besides core's (FORMATS)."""
    path = os.path.join(image_dir, "manifest.json")
    try:
        with open(path) as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        raise FlashError(f"{image_dir}: no readable manifest.json ({e})") from e
    missing = [k for k in ("family", "format", "file") if not manifest.get(k)]
    if missing:
        raise FlashError(f"{path}: missing {', '.join(missing)}")
    if manifest["format"] not in FORMATS + tuple(formats):
        raise FlashError(f"{path}: unknown format {manifest['format']!r}")
    file = os.path.join(image_dir, manifest["file"])
    if not os.path.isfile(file):
        raise FlashError(f"{path}: the image file {manifest['file']} is not there")
    return manifest, file


class NotOurs(FlashError):
    """The board does not answer as the family asked for: other firmware, a blank chip.
    Overwriting it is a first flash, which a layout must ask for by name."""


class Unanswered(FlashError):
    """The device is not there to read: its port holds nothing, its probe sees no core, its
    transport could not connect. Nothing was exchanged, so this says nothing about what it
    runs; a play skips such a board (modules say `unanswered: true`), where any other failure
    stops the bench."""


class Absent(str):
    """A reason that is devreg saying the device is registered but not on this host now. It
    reads as any other reason (a str); a caller that sees one has an unanswered board, not a
    refusal (nothing was exchanged with it)."""


#: devreg's exit status for a registered device it does not find (devreg.py NOTFOUND).
DEVREG_ABSENT = 1


def port_failure(name, why, before_write=True):
    """The error for a port lookup that gave `why`: a board devreg says is not on this host is
    unanswered (skipped) as long as nothing has been written to it; anything else, or the same
    after a write (which then goes unchecked), fails."""
    if isinstance(why, Absent) and before_write:
        return Unanswered(f"{name}: {why}")
    return FlashError(why)


def image_identity(manifest):
    key = familyspec.of(manifest["family"])["identity_key"]
    ident = identity.make(manifest["family"], **{k: manifest[k] for k in
                                                 ("target", "version", "hash", "build", key)
                                                 if k and manifest.get(k) is not None})
    # An image built from a recipe has exactly the features its manifest lists: none, if none.
    if familyspec.exact_features(manifest["family"]):
        ident["features"] = familyspec.canon_features(
            familyspec.feature_aliases(manifest["family"]), manifest.get("features"))
    return ident


def common_refusals(manifest, formats, board_tag=None, declared=None, at_base=False):
    """The checks that hold for any flasher: the image is of a kind this flasher writes, it
    is for this type of board, and it is what the layout declares for the board (`declared`,
    the board's `firmware:` block; the family says what must match: a class, a role, the
    features). An image staged with the address it goes at (`base`, an Intel HEX or ELF file
    converted: module_utils/imagefile.py) is written only by a flasher that writes there
    (`at_base`); any other would put it at the start of flash."""
    out = []
    if manifest["format"] not in formats:
        out.append(f"the image is {manifest['format']}; this flasher writes "
                   f"{', '.join(formats)}")
    if manifest.get("base") is not None and not at_base:
        out.append(f"the image goes at {manifest['base']} (staged from "
                   f"{(manifest.get('converted_from') or {}).get('format') or 'a file'} linked "
                   "there); this flasher writes from the start of flash only")
    if board_tag is not None and manifest.get("board_tag") != board_tag:
        out.append(f"the image is for a {manifest.get('board_tag')!r} board; this board is "
                   f"a {board_tag!r}")
    if declared is not None:
        out += familyspec.image_refusals(manifest.get("family"), manifest, declared)
    return out


#: How long a busy mark holds at most, should the run that wrote it die without ending it.
BUSY_TTL = 1800
#: How long a mark still holds once its flash is done: the board is still settling (rebooting
#: into its application, re-enumerating), and an apply right after starts its own flash a few
#: seconds later. The relay samples every 30 s; this covers two of its readings.
BUSY_GRACE = 75


def _busids(port):
    """The USB device a serial port sits on, as sysfs names it (`1-1.3`), or None."""
    path = os.path.realpath(os.path.join("/sys/class/tty", os.path.basename(port or "")))
    while path not in ("/", "/sys"):
        if os.path.exists(os.path.join(path, "idVendor")):
            return os.path.basename(path)
        path = os.path.dirname(path)
    return None


class busy:
    """While a board is being read or written, say so on its bench host
    (`<host state>/busy/<name>.json`: the board, the USB port it is on, its chip ids, until when).
    A flash makes a board leave the bus and come back, often as another USB device (its
    bootloader); the plug relay holds what it last reported for a busy board and its port
    instead of announcing every step. When the flash is done the mark is kept for BUSY_GRACE
    seconds more, not removed. Nothing is written where the host keeps no state directory (a
    machine that is not a bench host)."""

    def __init__(self, name, port=None, keys=()):
        self.name, self.port, self.keys = name, port, [k for k in keys if k]
        root = os.path.expanduser(os.environ.get("RXRBENCH_HOST_STATE", "~/rxrbench/state"))
        self.path = os.path.join(root, "busy", f"{name}.json") if name and os.path.isdir(root) else None

    def _write(self, until):
        bus = _busids(self.port)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".tmp", "w") as f:
            json.dump({"board": self.name, "busids": [bus] if bus else self.busids,
                       "keys": self.keys, "until": until}, f)
        os.replace(self.path + ".tmp", self.path)
        self.busids = [bus] if bus else self.busids

    def __enter__(self):
        self.busids = []
        if self.path:
            try:
                self._write(time.time() + BUSY_TTL)
            except OSError:
                self.path = None      # a mark that cannot be written only costs a notice
        return self

    def __exit__(self, *exc):
        if self.path:
            try:
                self._write(time.time() + BUSY_GRACE)
            except OSError:
                pass
        return False


def busy_marks(now=None):
    """(the USB ports, the chip ids, upper case) the live busy marks on this host cover: what a
    flasher holds now, so nothing else asks it (the survey, the plug watcher)."""
    root = os.path.join(os.path.expanduser(os.environ.get("RXRBENCH_HOST_STATE", "~/rxrbench/state")),
                        "busy")
    now = time.time() if now is None else now
    ports, keys = set(), set()
    for f in sorted(os.listdir(root)) if os.path.isdir(root) else ():
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(root, f)) as fh:
                mark = json.load(fh)
        except (OSError, ValueError):
            continue
        if float(mark.get("until") or 0) >= now:
            ports |= {str(b) for b in mark.get("busids") or () if b}
            keys |= {str(k).upper() for k in mark.get("keys") or () if k}
    return ports, keys


class Flasher:
    """What `drive` asks of a flasher. A subclass reads the board (`device`), says what stops a
    write (`preflight`, and the refusals of a first flash or a family switch when it has those
    paths), writes and reads back; the rest has a default here.

        name      the board, for its busy mark (None: no mark)
        b         its broker client, when the board has a console the bench speaks: what
                  else it runs is asked there when it does not answer as the image's family
        devreg    devreg on the host, to find the board's USB port for the busy mark
        chip_id   a key the busy mark carries besides the name
        fresh     the write leaves none of the board's config (it boots on its image's radio)
        deferred  what a plan could not check without a reset, said with the plan
        spot_reads the reads that have a spot form (docs/ARCHITECTURE.md#checking-depth); none by default,
                  so every read is full whatever is asked. `before write`: the flasher's
                  preflight reads less (its own design); `identity`: in a plan, drive() may
                  take the board's last full read instead of `device()` (lastread.py)
        checked   the depth `drive` chose for the checks before a write ({depth, reason}),
                  set before `preflight` runs
        first     whether `drive` found a first flash (the board answers as no known family),
                  set before `preflight` runs: the write is then the first-flash one
    """
    name = None
    b = None
    devreg = None
    chip_id = None
    fresh = False
    deferred = None
    spot_reads = ()
    checked = None
    first = False
    #: the family the board was read as running before the write (None: a first flash, or not
    #: read), set by drive() before preflight
    running_family = None
    #: The chip's declared ranges (bench_flash_regions) and the ones the layout lets a write
    #: take (bench_overwrite). A flasher that also gives read_range(start, length) and
    #: write_ranges() (absolute addresses) is asked the write rule by drive(), with no code of
    #: its own (docs/ARCHITECTURE.md#partitions-and-the-write-rule).
    flash_regions = ()
    overwrite = ()

    def device(self):
        raise NotImplementedError

    def preflight(self):
        return []

    def depth_moments(self):
        """The moments that force a full read (docs/ARCHITECTURE.md#checking-depth) this write is, beyond a first flash or a family switch
        (which drive() knows): {"table_change": bool, "erase": bool, "no_full_record": bool}.
        Asked after device(), before the depth is resolved. Generic: none."""
        return {}

    def first_flash_refusals(self):
        return [f"{type(self).__name__} has no first-flash path"]

    def first_flash_used(self):
        """Whether its own read (`device`) found a chip that only a first flash may write,
        where `drive`'s test (the board does not answer as its family) does not see it: the
        whole-image writer reads another firmware's partition table."""
        return False

    def family_switch_refusals(self, before, check_mode=False):
        return [f"{type(self).__name__} cannot switch a board between firmware families"]

    def write(self):
        raise NotImplementedError

    def write_first(self):
        raise NotImplementedError

    def verify(self):
        raise NotImplementedError

    def leave(self):
        """Put the board back where the run found it (picoboot: out of its bootloader)."""


def drive(flasher, manifest, check_mode=False, first_flash=False, flash_unknown=True,
          boot_radio=None, boot_radio_ok=False, check_depth="full"):
    """The four steps (`_drive`), with the board marked busy on its host while they run. The
    result's `timing` holds each phase's duration (module_utils/timing.py)."""
    phases = timing.Phases()
    port = None
    if flasher.name and flasher.devreg and os.path.exists(flasher.devreg):
        with phases.phase("resolve"):
            rc, out = tool(["python3", flasher.devreg, "resolve", flasher.name], 30)
        port = out.strip().splitlines()[-1] if rc == 0 and out.strip() else None
    with busy(flasher.name, port, keys=[flasher.chip_id]):
        try:
            result = _drive(flasher, manifest, check_mode, first_flash, flash_unknown,
                            boot_radio, boot_radio_ok, phases, check_depth, _busids(port))
        finally:
            # a flasher that put the board somewhere for the run puts it back while it is
            # still marked busy
            with phases.phase("leave"):
                flasher.leave()
    result["timing"] = phases.out
    return result


def boot_radio_refusal(boot_radio, ok):
    """A fresh write (a first flash, a family switch, a whole chip of another firmware) leaves the
    board on its image's own radio until its config is written: `boot_radio` holds what the
    layout check found wrong with that against the site's RF rules. Refused unless the layout
    says the board may transmit there for that moment (`bench_boot_radio_ok`: a dummy load, a
    shielded box)."""
    if not boot_radio or ok:
        return []
    return [f"its first boot on this image would transmit before its config is written: "
            f"{'; '.join(boot_radio)}. Write it only with the antenna on a dummy load or "
            "shielded, and say so with bench_boot_radio_ok: true for this board"]


def _drive(flasher, manifest, check_mode=False, first_flash=False, flash_unknown=True,
           boot_radio=None, boot_radio_ok=False, phases=None, check_depth="full", busid=None):
    """Run the four steps. Returns {"changed", "before", "after", "image", "why"}; raises
    FlashError with every refusal, or when the device does not run the image afterwards.

    A board that does not answer as the image's family (NotOurs) is only written with
    `first_flash`, and then through the flasher's own first-flash refusals and write (an
    ESP32 is erased whole and gets its bootloader too; an nRF52840 must run the bootloader its board type requires first).

    A board that answers as its family but reports neither a build nor a hash (firmware
    older than the identity it would be compared by) is written forward, unless the layout
    says `bench_flash_unknown: false`: then it is refused, and nothing is written to a board
    whose image cannot be named."""
    image = image_identity(manifest)
    # An image that carries neither a hash nor a build id can never be told from what a board
    # runs: every apply would write it again, and no read-back could prove the write. Refused
    # before the device is asked anything.
    if not any(image.get(f) is not None for f in identity.STRONG):
        raise FlashError("refused: the manifest has no build or hash to compare, so nothing "
                         "could tell whether the device runs this image, before a write or after")
    phases = phases or timing.Phases()
    first = switch = False
    kind = type(flasher).__name__
    # What the board runs: asked, or in a plan at spot its last full read when nothing can have
    # changed it through USB since (docs/ARCHITECTURE.md#checking-depth).
    ident_checked = depth.resolve(check_depth, read="identity", spot_reads=flasher.spot_reads,
                                  apply=not check_mode)
    if ident_checked["depth"] == "spot":
        rec, why = lastread.stand_in(flasher.name, kind, manifest["family"], busid)
        if rec is not None and identity.same(rec, image) is True:
            # no write, so no checks before one; a record shows the image, never another
            # firmware's table, so a first-flash go-ahead is not needed
            return {"changed": False, "image": image, "before": rec, "after": rec,
                    "checked": {"depth": "spot", "reason": "nothing to write: no checks before a write"},
                    "identity_checked": {"depth": "spot", "reason": why},
                    "first_flash_unneeded": bool(first_flash)}
        ident_checked = {"depth": "full", "reason": why if rec is None else
                         "the last full read shows another image than the declared one"}
    try:
        with phases.phase("read"):
            before = flasher.device()
    except NotOurs as e:
        lastread.forget(flasher.name)
        # Not the image's family: perhaps another known family (a board going from
        # one firmware to another), which is a family switch, not a first flash.
        try:
            if flasher.b is None:     # no console to ask what else it runs
                raise NotOurs(str(e))
            with phases.phase("read other families"):
                before = read_board_any(flasher.b, exclude=manifest["family"])
            switch = True
        except NotOurs:
            if not first_flash:
                raise FlashError(f"{e}. Overwriting it is a first flash: declare "
                                 "bench_first_flash: true for this board") from e
            before, first = identity.make("foreign"), True
    except Exception:
        lastread.forget(flasher.name)
        raise
    else:
        if "identity" in flasher.spot_reads:
            lastread.record(flasher.name, kind, manifest["family"], before, busid)
    # How much the checks before a write read (docs/ARCHITECTURE.md#checking-depth): what was asked,
    # unless this moment forces full. The flasher's preflight reads at this depth.
    flasher.checked = depth.resolve(check_depth, read="before write", spot_reads=flasher.spot_reads,
                                    first_flash=first, family_switch=switch,
                                    **flasher.depth_moments())
    flasher.first = first
    flasher.running_family = None if first else before.get("family")
    match = identity.same(before, image)
    result = {"changed": False, "image": image, "before": before, "after": before,
              "checked": flasher.checked, "identity_checked": ident_checked,
              # declared, but the board answers as its family: the go-ahead is left over from
              # an earlier first flash, and would let a board that stops answering be overwritten
              "first_flash_unneeded": bool(first_flash and not first
                                           and not flasher.first_flash_used())}
    if match is True:
        return result
    result["why"] = ("the device does not say enough about itself to tell whether it runs "
                     "this image" if match is None else
                     ", ".join(f"{k} is {a!r}, the image has {b!r}"
                               for k, (a, b) in identity.differences(before, image).items()))
    if match is None and not first and not switch and not flash_unknown:
        raise FlashError("refused: the device reports no build or hash, so nothing can tell "
                         "whether it runs this image, and bench_flash_unknown is false for "
                         "this board. Set it true to write it forward")
    if first:
        result["why"] = "first flash: the board does not run this firmware"
        result["first"] = True
    if switch:
        result["why"] = f"family switch: {before.get('family')} -> {image.get('family')}"
        result["switch"] = True
    with phases.phase("preflight"):
        refusals = flasher.preflight() + (flasher.first_flash_refusals() if first else []) + (
            flasher.family_switch_refusals(before, check_mode=check_mode) if switch else [])
        refusals += declared_range_refusals(flasher)
    # a write that leaves none of the board's config: it boots on the image's own radio
    if first or switch or flasher.fresh:
        refusals += boot_radio_refusal(boot_radio, boot_radio_ok)
    absent = [r for r in refusals if isinstance(r, Absent)]
    if absent:      # not there to read: skipped, not refused
        raise Unanswered("; ".join(absent))
    if refusals:
        raise FlashError("refused: " + "; ".join(refusals))
    if flasher.deferred:      # a check a plan could not make without a reset
        result["why"] += "; " + flasher.deferred
    result["changed"] = True
    if check_mode:
        result["after"] = {**before, **{k: v for k, v in image.items() if v is not None}}
        return result
    # a write in progress, or one that fails, leaves no last full read to stand in for the board
    lastread.forget(flasher.name)
    with phases.phase("write"):
        if first:
            flasher.write_first()
        else:
            flasher.write()
    with phases.phase("verify"):
        after = flasher.verify()
    result["after"] = after
    if identity.same(after, image) is not True:
        diffs = identity.differences(after, image)
        raise FlashError("the device does not run the image after the flash: " + (
            ", ".join(f"{k} is {a!r}, wanted {b!r}" for k, (a, b) in diffs.items())
            or "it cannot be told apart"))
    if "identity" in flasher.spot_reads:
        lastread.record(flasher.name, kind, manifest["family"], after, busid)
    return result


def stale_first_flash(module, board, r):
    """Say so when the layout declares a first flash for a board that answers as its family."""
    if r.get("first_flash_unneeded"):
        module.warn(f"{board} answers as its declared firmware, but bench_first_flash is still "
                    "set for it: drop it, or a board that stops answering (a halted loader, "
                    "another firmware) is overwritten without a question")


# ---- what the flashers of real boards share -------------------------------------------

def _read_identity(board, family, spec, deadline):
    """What a board says it runs, read as its family's `identity` spec says (familyspec.py)."""
    who = board.board
    kw = {"dialect": spec["dialect"]} if spec.get("dialect") else {}
    try:
        # a family that prints no state line answers its fallback command alone
        j = None if spec.get("lineless") else stateline.read(board, spec, deadline)
    except broker_client.BrokerError as e:
        # A port the broker holds nothing on is a board that is not there (unplugged, or
        # behind a bridge the watcher cannot see it by): nothing to read, nothing to say
        # about what it runs.
        # A write that times out is a port that is there with nothing reading it (a board
        # stuck in a bootloader with no app it can boot): not absent, not this family.
        if "port closed" in str(e) and "Write timeout" not in str(e):
            raise Unanswered(f"{who}: its port is not open ({e})") from e
        if "Write timeout" in str(e):
            raise NotOurs(f"{who} takes no input on its console ({e}): no firmware of "
                          f"{family} answers there") from e
        # A console that does not echo, or never ends its answer the way this family's does,
        # is another firmware's (or a bootloader's port); drive() then asks the other
        # families. A board of this family that is only slow fails this too, and is refused.
        if "no echo" in str(e) or "timeout" in str(e):
            raise NotOurs(f"{who} does not answer as {family} firmware ({e})") from e
        raise
    if j is None:
        fallback = spec.get("fallback")
        if not fallback:
            raise NotOurs(f"{who} does not answer as {family} (no `{spec['command']}` line)")
        try:
            out = board.exec(fallback["command"], deadline=deadline, **kw)
        except broker_client.BrokerError as e:
            if not spec.get("lineless") or not ("no echo" in str(e) or "timeout" in str(e)):
                raise
            raise NotOurs(f"{who} does not answer as {family} firmware ({e})") from e
        if fallback["require"] not in out:
            raise NotOurs(f"{who} does not answer as {family} firmware (no state line, "
                          f"no `{fallback['command']}`)")
        return identity.make(family, **familyspec.fields_of_fallback(fallback, out))
    if spec.get("version") is not None and j.get("v") != spec["version"]:
        raise stateline.StateError(f"state version {j.get('v')!r}, this tool reads {spec['version']}")
    # A line that names its family is proof only when it names this one.
    if spec.get("names_family") and j.get("family") != family:
        raise NotOurs(f"{who} does not answer as {family} (no `{spec['command']}` line naming it)")
    return identity.make(family, **familyspec.fields_of_line(spec, j))


def declared_range_refusals(flasher):
    """The write rule over a chip's declared ranges (no partition table, no flash map): the
    flasher's own read of each app region the write may destroy, at the depth the checks read,
    and a bootloader range protected unless bench_overwrite names it. [] for a flasher without
    declared ranges or without a read."""
    ranges = list(flasher.flash_regions or ())
    if not ranges or not callable(getattr(flasher, "read_range", None)) \
            or not callable(getattr(flasher, "write_ranges", None)):
        return []
    writes = list(flasher.write_ranges())
    inv = regions.inventory(ranges, flasher.read_range, writes,
                            depth=(flasher.checked or {}).get("depth", "full"), writes=writes)
    return regions.refusals(inv, writes, allowed=tuple(flasher.overwrite or ()), protect=("boot",))


def read_board_any(board, exclude=None, deadline=10.0):
    """What a board runs, asking as each known family in turn (every family with a state
    line but `exclude`): for a board that may run another family than the one about to be
    written. NotOurs when none answers."""
    tried = []
    for fam in familyspec.with_identity(exclude):
        try:
            return read_board(board, fam, deadline)
        except Unanswered:
            raise           # not there at all: no family would answer, say so as such
        except (FlashError, broker_client.BrokerError, stateline.StateError) as e:
            tried.append(f"{fam}: {e}")
    raise NotOurs(f"{board.board} answers as none of the firmware "
                  f"families known here ({'; '.join(tried) or 'none to ask'})")


def runs_instead(board, declared, deadline=10.0):
    """When a board did not answer as its declared family: what it runs instead, said for a
    read-only step (`just facts`), or None when no other family answers either: a board switched
    by snapshot to another family, its layout not yet changed, is told as what it runs, not as
    the declared family's state command timing out."""
    try:
        now = read_board_any(board, exclude=declared, deadline=deadline)
    except (FlashError, broker_client.BrokerError, stateline.StateError):
        return None
    what = " ".join(str(x) for x in (now.get("family"), now.get("version") or now.get("build"))
                    if x)
    return (f"it runs {what}, not the {declared} its layout declares: `just plan` shows the "
            f"switch apply would make; declare {now.get('family')} to read it here")


#: Where a bench host keeps devreg and devlock, and the tools plugins deploy (`hosts-setup`).
DEVREG_DIR, PLUGIN_BIN = "~/.devreg", "~/rxrbench/bin"


def read_board(board, family, deadline=10.0):
    """What a board of `family` says it runs: from its state line, through its broker, or for
    firmware with none, from the probe tool its family names."""
    spec = familyspec.of(family)["identity"]
    if spec is None:
        raise FlashError(f"no way to read what a {family!r} board runs: its family has no "
                         f"state command this tool reads{familyspec.unloaded(family)}")
    if spec.get("tool"):
        return read_by_tool(board, family, spec)
    return _read_identity(board, family, spec, deadline)


class PortConsole(broker_client.NoBroker):
    """A board no broker owns, asked on its port itself, as the survey does (usb_survey's
    console_exchange): the port resolved and guarded by name, under its device lock, opened
    exclusively, read until a state line is there or the time is up. So a board whose broker
    is gone (its layout now declares a family with no console) still says what it runs, and a
    flash can tell a family switch from unknown firmware. Nothing to release or take back."""

    def __init__(self, board, path=None, devreg_dir=DEVREG_DIR, serial_open=None, **_kw):
        super().__init__(board, path)
        self.devreg_dir = os.path.expanduser(devreg_dir)
        self._open = serial_open

    def exec(self, cmd, deadline=10.0, dialect=None, **_kw):  # pylint: disable=unused-argument
        port, why = guarded_port(os.path.join(self.devreg_dir, "devreg.py"), self.board)
        if why:
            raise broker_client.BrokerError(f"{self.board}: {why}")
        if self.devreg_dir not in sys.path:
            sys.path.insert(0, self.devreg_dir)
        from devlock import DevLock  # pylint: disable=import-error,import-outside-toplevel
        with DevLock(port, tool="rxrbench-console") as lk:
            if not lk.held:
                raise broker_client.BrokerError(f"{self.board}: port busy: {lk.holder_summary()}")
            if self._open is None:
                import serial  # pylint: disable=import-outside-toplevel

                def opener(path):
                    return serial.Serial(path, 115200, timeout=0.2, exclusive=True)
            else:
                opener = self._open
            with opener(port) as s:
                s.reset_input_buffer()
                s.write(b"\r\n" + cmd.encode() + b"\r\n")
                buf, end = b"", time.monotonic() + min(deadline, 6.0)
                while time.monotonic() < end:
                    buf += s.read(4096)
                    if b"~S{" in buf and stateline.STATE_LINE.search(buf.decode(errors="replace")):
                        break
        text = buf.decode(errors="replace")
        if cmd not in text and not stateline.STATE_LINE.search(text):
            # what the broker says of a console that never echoed the command
            raise broker_client.BrokerError(f"{self.board}: `{cmd}`: no echo")
        return text


def read_by_tool(board, family, spec, devreg_dir=DEVREG_DIR, plugin_bin=PLUGIN_BIN, run=None):
    """A board of a family with no state line, asked by the probe its plugin deploys (one JSON
    object on stdout): on the port devreg resolves and guards for the board's name, under the
    port's device lock (a port another holds, a broker among them, is not this family's to
    ask), in a child process with a deadline (a library that hangs is killed).

    spec: tool (the script), fields {identity field: probe key}, build_from {key, pattern}
    (a build id inside another value: a release version that ends in its commit), timeout."""
    name = board.board
    devreg_dir = os.path.expanduser(devreg_dir)
    port, why = guarded_port(os.path.join(devreg_dir, "devreg.py"), name)
    if isinstance(why, Absent):
        raise Unanswered(f"{name}: {why}")
    if why:
        raise NotOurs(f"{name}: {why}")
    if devreg_dir not in sys.path:
        sys.path.insert(0, devreg_dir)
    from devlock import DevLock  # pylint: disable=import-error,import-outside-toplevel
    script = os.path.join(os.path.expanduser(plugin_bin), spec["tool"])
    # A board declared as a console family has a broker on its port even while it runs this
    # firmware (a switch back): the broker lets go for the probe, as for a flash, and takes
    # the port back after. A board with no broker has nothing to let go.
    released = False
    try:
        board.release(ttl=spec.get("timeout", 60) + 30)
        released = True
    except broker_client.BrokerError:
        pass
    try:
        with DevLock(port, tool=f"rxrbench-probe-{family}") as lk:
            if not lk.held:
                raise NotOurs(f"{name}: its port is held ({lk.holder_summary()}): not asked as {family}")
            rc, out = (run or tool)([sys.executable, script, port], timeout=spec.get("timeout", 60))
    finally:
        if released:
            try:
                board.reacquire()
            except broker_client.BrokerError:
                pass
    lines = [ln for ln in out.splitlines() if ln.startswith("{")]
    if rc != 0 or not lines:
        raise NotOurs(f"{name} does not answer as {family} ({spec['tool']} rc {rc}: "
                      f"{out.strip()[-160:]})")
    j = json.loads(lines[-1])
    fields = {f: j[k] for f, k in (spec.get("fields") or {}).items() if j.get(k) is not None}
    b = spec.get("build_from")
    if b and j.get(b["key"]):
        m = re.search(b["pattern"], str(j[b["key"]]))
        if m:
            fields["build"] = m.group(1)
    if not fields:
        raise NotOurs(f"{name} answers {spec['tool']} but says nothing of what it runs")
    return identity.make(family, **fields)


def wait_back(board, family, settle, deadline=10.0):
    """Take the console back and read the board, for up to `settle` seconds. The console
    was released for the write, so the first answer here is from after the reset.

    A board that is up may still not echo for a while (its console is also its log sink:
    docs/HARDWARE.md), so the wait goes on until `settle` runs out."""
    start = time.monotonic()
    end = start + settle
    why = "the board did not come back"
    busy = 0
    while time.monotonic() < end:
        try:
            # a family with no console has no broker to take the port back
            r = board.reacquire() if familyspec.of(family)["console"] else {"ok": True}
            if r.get("ok"):
                return read_board(board, family, deadline)
            why = r.get("error") or why
        except (broker_client.BrokerError, stateline.StateError, FlashError) as e:
            why = str(e)
            if "no echo" in why:
                busy += 1
        time.sleep(0.5)
    hint = (" (the console answered without echoing the commands throughout: a boot-time log "
            "flood is the usual cause; a longer `settle` or a quieter log level helps)"
            if busy else "")
    raise FlashError(f"after the flash, {int(time.monotonic() - start)} s: {why}{hint}")


def app_running(board, family, deadline=10.0):
    """Does the board's app answer as its family?"""
    try:
        return read_board(board, family, deadline) is not None
    except Exception:  # pylint: disable=broad-except
        return False


def came_back(board, family, settle):
    """The app answers again after a reset (wait_back, as a yes or no)."""
    try:
        return wait_back(board, family, settle) is not None
    except FlashError:
        return False


def tool(argv, timeout, env=None, cwd=None):
    """Run a flashing tool. Returns (rc, output); a tool that does not finish is killed and
    reported, never waited for."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                           env={**os.environ, **(env or {})}, cwd=cwd)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"") + (e.stderr or b"")
        return 124, (out.decode(errors="replace") if isinstance(out, bytes) else out) + \
            f"\n(killed after {timeout} s)"
    except OSError as e:
        return 127, str(e)


def read_flash(esptool, chip, port, offset, length, after="no-reset", timeout=120):
    """`length` bytes of the chip's flash from `offset`, through esptool. `after` says what the
    chip does afterwards: `no-reset` leaves it in its loader for the next esptool call in the
    same session, `hard-reset` boots it."""
    import tempfile  # pylint: disable=import-outside-toplevel
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "flash.bin")
        rc, out = tool([esptool, "--chip", chip, "--port", port, "--after", after, "read-flash",
                        f"{offset:#x}", f"{length:#x}", path], timeout=timeout)
        if rc != 0 or not os.path.exists(path):
            raise FlashError(f"esptool read-flash {offset:#x} failed (rc {rc}): "
                             + out.strip()[-300:])
        with open(path, "rb") as f:
            return f.read()


def read_manager(esptool, chip, port, after="no-reset"):
    """The app manager the chip carries (`esppart.manager`), read from its partition table.
    None when there is none, or no table at all."""
    data = read_flash(esptool, chip, port, esppart.OFFSET, esppart.LENGTH, after=after)
    return esppart.manager(esppart.parse(data))


def overwrite_allowed(overwrite=(), manager_overwrite=False):
    """The regions a write may take (bench_overwrite), with bench_app_manager_overwrite as all."""
    return ("all",) if manager_overwrite else tuple(overwrite or ())


#: A write that erases the chip: it covers every region, whatever its size.
WHOLE_CHIP = [(0, 1 << 30)]


def flash_map_regions(fj):
    """The regions of an MCUboot layout from an image's flash.json (docs/ARCHITECTURE.md#partitions-and-the-write-rule,
    "A chip with no partition table"): its `map` (NAME / NAME_SIZE pairs), else its
    `partitions` (slot0, slot1, storage, storage_size). SLOT* are app regions, BOOT the
    bootloader's, the rest data. [] when the file carries neither."""
    fj = fj or {}
    m = fj.get("map") or {}
    out = []
    if m:
        for name, start in m.items():
            size = m.get(name + "_SIZE")
            if name.endswith("_SIZE") or name == "FLASH" or size is None:
                continue
            kind = "app" if name.startswith("SLOT") else "boot" if name == "BOOT" else "data"
            out.append({"start": int(str(start), 16), "size": int(str(size), 16),
                        "label": name.lower(), "kind": kind})
    else:
        parts = fj.get("partitions") or {}
        offs = sorted((int(str(v), 16), k) for k, v in parts.items() if not k.endswith("_size"))
        for i, (start, name) in enumerate(offs):
            size = parts.get(name + "_size")
            end = int(str(size), 16) + start if size else (offs[i + 1][0] if i + 1 < len(offs) else None)
            if end is None:
                continue
            out.append({"start": start, "size": end - start, "label": name,
                        "kind": "app" if name.startswith("slot") else "data"})
    return sorted(out, key=lambda r: r["start"])


def esp_write_refusals(esptool, chip, port, writes, allowed=(), check_depth="full",
                       what="this write", new_table=None, blocks=None, flash_map=None):
    """The write rule on an ESP32 (docs/ARCHITECTURE.md#partitions-and-the-write-rule), read from the chip in the
    session the write follows: its partition table, its otadata, and the start of each app
    region (at `check_depth`). The firmware the chip runs (esppart.firmware_apps) is the
    layout's to replace; a write that would destroy any other image, or a data region of a chip
    with a manager (shared by every app), is refused unless `allowed` names its region (or is
    ("all",)). A chip with no ESP-IDF table (an MCUboot
    layout, a blank chip) has no regions here to compare: [].

    writes     [(offset, length)] the write covers (WHOLE_CHIP for an erase)
    new_table  the partition table the write puts at 0x8000, when it writes one: each region
               of the chip's table it drops is compared as if the write covered it
    blocks     a callable that runs chip_blocks.py (flash/chip.py `blocks`, its tools bound):
               then everything is read in one loader session (`layout`); without it each
               range is one esptool read
    flash_map  the image's own regions (flash_map_regions), for a chip with no table (an
               MCUboot layout): an MCUboot image in a slot is the layout's, any other foreign"""
    apps_of_map = [r for r in flash_map or [] if r["kind"] == "app"]
    def session(only):
        """(table, otadata, read): one loader session's view of the chip; `only`: the ranges
        whose app heads to read (a spot check), else every head."""
        if blocks is None:
            return None, None, lambda off, n: read_flash(esptool, chip, port, off, n)
        ranges = ",".join(f"{s:#x}:{s + n:#x}" for s, n in only or [])
        at = [r["start"] for r in apps_of_map
              if not only or regions.overlaps(r, only)]
        got = blocks(port, "layout", *([ranges] if ranges else []),
                     *(["--at", ",".join(f"{a:#x}" for a in at)] if at else []), timeout=120)
        heads = {int(k, 16): bytes.fromhex(v) for k, v in (got.get("heads") or {}).items()}
        return (bytes.fromhex(got["table"]),
                bytes.fromhex(got["otadata"]) if got.get("otadata") else None,
                lambda off, n: heads[off][:n] if off in heads else read_flash(esptool, chip, port, off, n))

    # spot (docs/ARCHITECTURE.md#checking-depth): the heads of the app regions the write overlaps alone, the
    # only ones it can destroy. A new table that drops a region is a table change, which reads
    # in full: then the chip is read again with every head.
    spot = check_depth == "spot"
    table, otadata, read = session(list(writes) if spot else None)
    rows = esppart.parse(table if table is not None else read(esppart.OFFSET, esppart.LENGTH))
    if not rows:
        return _map_refusals(flash_map, read, writes, allowed, check_depth, what)
    new_rows = esppart.parse(new_table) if new_table else []
    drops = (regions.dropped(regions.from_esp_table(rows), regions.from_esp_table(new_rows))
             if new_rows else [])
    if spot and drops:
        check_depth = "full"
        table, otadata, read = session(None)
        rows = esppart.parse(table if table is not None else read(esppart.OFFSET, esppart.LENGTH))
    ota = next((r for r in rows if r["type"] == esppart.DATA and r["subtype"] == esppart.SUB_OTADATA),
               None)
    if ota and otadata is None:
        otadata = read(ota["offset"], min(ota["size"], 0x2000))
    inv = esppart.inventory(rows, otadata, read, depth=check_depth, writes=list(writes) + drops)
    return [f"{what}: {why}" for why in regions.refusals(inv, writes, allowed, drops)]


def recorded_write_refusals(record, tags, writes, allowed=(), what="this write", new_table=None):
    """The write rule over the survey's record of the chip, for the checks before a write that
    read nothing from it (a plan's; the apply asks the chip itself, esp_write_refusals, just
    before it writes). The same comparison, so the plan and the apply agree and both honour
    `allowed` (bench_overwrite; bench_app_manager_overwrite is ("all",)).

    record  the esptool probe of the board's facts: {partitions: {rows, inventory, ...},
            gathered}; empty when the board was never surveyed deep, then nothing is known
    tags    the facts' tags: a record from before the survey kept an inventory still says
            `app-manager:<label>`, and that answer stands (refused unless all; survey again)

    Other arguments as esp_write_refusals'."""
    record = record or {}
    parts = record.get("partitions") or {}
    inv = [{"start": r["offset"], "size": r["size"], "label": r["label"], "kind": r["kind"],
            "owner": r.get("owner"), "evidence": {"recogniser": r.get("recogniser")}}
           for r in parts.get("inventory") or []]
    if inv:
        # the regions every ESP-IDF chip has before its table's rows (the bootloader's space,
        # the table itself), which the survey does not record: as the chip's rule knows them
        inv = [{**r, "owner": "unknown", "evidence": {"how": "fixed"}}
               for r in regions.from_esp_table([])] + inv
    when = record.get("gathered") or "on an unknown date"
    if not inv:
        tag = next((t for t in tags or () if t.startswith("app-manager:")), None)
        if not tag or "all" in allowed:
            return []
        return [f"{what}: the survey found an app manager in {tag.split(':', 1)[1]} on this chip "
                "but kept no record of its regions: survey it again (`just survey <host> true`) "
                "so the plan can say what the write takes, or `bench_app_manager_overwrite: true` "
                "for this board if every region is meant"]
    drops = []
    new_rows = esppart.parse(new_table) if new_table else []
    if new_rows:
        drops = regions.dropped(inv, regions.from_esp_table(new_rows))
    return [f"{what}: {why} (as surveyed {when})"
            for why in regions.refusals(inv, writes, allowed, drops)]


def _map_refusals(flash_map, read, writes, allowed, check_depth, what):
    """The rule on a chip with no table, over the image's map: an MCUboot image in a slot is the
    layout's, any other recognised image foreign. No map: nothing to compare ([])."""
    if not flash_map:
        return []
    inv = regions.inventory(flash_map, read, declared=[], depth=check_depth, writes=list(writes))
    for r in inv:
        if r.get("owner") == "foreign" and (r.get("evidence") or {}).get("recogniser") == "mcuboot-image":
            r["owner"] = "declared"
        elif r["kind"] == "data":
            r["owner"] = "declared"
    return [f"{what}: {why}" for why in regions.refusals(inv, writes, allowed)]


def manager_refusal(mgr, allowed, what="this write"):
    """The one refusal every ESP32 writer shares: a chip that carries an app manager with
    installed apps is not written at 0x0 or into its slots unless the layout says so
    (`bench_app_manager_overwrite`), since the write orphans every app."""
    if not mgr or allowed:
        return None
    return (f"the chip carries {esppart.describe(mgr)}: {what} would orphan every installed "
            "app; `bench_app_manager_overwrite: true` for this board if that is meant")


def manager_tag_refusal(tags, allowed, what="this write"):
    """The same refusal from the survey's record (`app-manager:<label>`), for a plan, which
    reads nothing from the chip."""
    tag = next((t for t in tags or () if t.startswith("app-manager:")), None)
    if not tag or allowed:
        return None
    apps = next((t.split(":", 1)[1] for t in tags if t.startswith("app-manager-apps:")), "?")
    return (f"the survey found an app manager in {tag.split(':', 1)[1]} on this chip ({apps} "
            f"apps installed): {what} would orphan every installed app; "
            "`bench_app_manager_overwrite: true` for this board if that is meant")


def guarded_port(devreg, name):
    """The board's port by name, and proof that the path is that board: devreg resolve, then
    devreg guard. Returns (port, None) or (None, why)."""
    rc, out = tool(["python3", devreg, "resolve", name], 30)
    port = out.strip().splitlines()[-1] if out.strip() else ""
    if rc != 0 or not port.startswith("/dev/"):
        why = f"devreg cannot resolve {name}: {out.strip()[-160:]}"
        return None, (Absent(why) if rc == DEVREG_ABSENT else why)
    rc, out = tool(["python3", devreg, "guard", name, port], 30)
    if rc != 0:
        return None, f"devreg guard refuses {port} for {name}: {out.strip()[-160:]}"
    return port, None


def route_refusal(devreg, name, bearer, address):
    """Why `address` may not be used to reach the board over `bearer`, or None: the registry
    on this host must record that address for that route (devreg guard --route). The path
    check's twin for a route that has no path, made before any session is opened."""
    if not address:
        return f"{name}: no {bearer} address to check against the registry"
    rc, out = tool(["python3", devreg, "guard", "--route", bearer, name, str(address)], 30)
    if rc != 0:
        return (f"devreg guard refuses {address} for {name} over {bearer}: "
                f"{out.strip()[-200:]}")
    return None


# An app on an ESP32-S3's native USB (TinyUSB CDC): the serial is the MAC
# without colons. esptool cannot work on this port: its first connection resets the chip into
# ROM download mode, where it is the USB-Serial-JTAG device 303a:1001 under another by-id
# name (the MAC with colons), and the port it was opened on is gone.
STOCK_ESP_CDC = ("303a", "0002")


def usb_face(port, sysfs="/sys"):
    """(vid, pid, serial) of the USB device behind a tty, from sysfs; None when it cannot be
    told (not a USB tty, or no sysfs: a fake port in a test)."""
    name = os.path.basename(os.path.realpath(port))
    start = os.path.join(sysfs, "class", "tty", name, "device")
    if not os.path.exists(start):
        return None
    path, root = os.path.realpath(start), os.path.realpath(sysfs)
    while path not in ("/", root):
        if os.path.exists(os.path.join(path, "idVendor")):
            def rd(n):
                try:
                    with open(os.path.join(path, n)) as fh:
                        return fh.read().strip()
                except OSError:
                    return ""
            return rd("idVendor").lower(), rd("idProduct").lower(), rd("serial")
        path = os.path.dirname(path)
    return None


def rom_port_path(mac):
    """The by-id path an ESP32-S2/S3 has in ROM download mode (303a:1001): its serial is the
    base MAC with colons, whatever the app's own USB identity was."""
    return f"/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_{mac.upper()}-if00"


# USB-UART bridges (hwsurvey.KINDS' uart-bridge): the board behind one is not told by USB. A
# CH340 has no serial at all, so its registry entry may match it by VID:PID alone.
UART_BRIDGES = {("10c4", "ea60"), ("1a86", "7523"), ("1a86", "55d4")}
MAC_LINE = re.compile(r"^MAC:\s*((?:[0-9a-f]{2}:){5}[0-9a-f]{2})", re.I | re.M)


def bridge_mac_refusal(devreg, name, port, esptool, chip):
    """Why the chip behind a bridge port is not the board, or None. When the registry holds the
    board's MAC (`policy.chip_mac`), the ROM loader reads the chip's own before esptool is let
    write: a bridge matched by VID:PID alone can be another board's (the board unplugged, a
    stranger of the same make in its place). With no MAC on record there is nothing to compare,
    and the match alone stands, as before."""
    rc, out = tool(["python3", devreg, "policy", name, "chip_mac"], 30)
    want = out.strip().lower() if rc == 0 else ""
    if not want:
        return None
    rc, out = tool([esptool, "--chip", chip, "--port", port, "--after", "hard-reset", "read-mac"], 60)
    got = MAC_LINE.search(out)
    if rc != 0 or not got:
        return (f"{name}: cannot read the MAC of the chip behind {port} to check it is {want} "
                f"(policy.chip_mac): {out.strip()[-160:]}")
    if got.group(1).lower() != want:
        return (f"{name}: the chip behind {port} is {got.group(1).lower()}, not {want} "
                "(policy.chip_mac): another board on a bridge of the same make; refused")
    return None


def esp_port(devreg, name, esptool, chip, sysfs="/sys", wait=20):
    """The port esptool can use for the board, guarded. The board's own port when it is the
    USB-Serial-JTAG face (an app, ROM mode: one face throughout);
    for a stock app on native USB, the chip is put into ROM download mode first (the way the
    survey does it) and the ROM port, guarded in its turn, is returned. The caller's own esptool
    run ends with its --after, which boots whatever is then in flash. Returns (port, None) or
    (None, why)."""
    port, why = guarded_port(devreg, name)
    if why:
        return None, why
    face = usb_face(port, sysfs)
    if face and (face[0], face[1]) in UART_BRIDGES:
        why = bridge_mac_refusal(devreg, name, port, esptool, chip)
        return (None, why) if why else (port, None)
    if not face or (face[0], face[1]) != STOCK_ESP_CDC:
        return port, None
    serial = face[2]
    if not re.fullmatch(r"[0-9A-Fa-f]{12}", serial):
        return None, (f"{name}: {port} is a stock app on native USB whose serial {serial!r} is not "
                      f"the MAC: cannot tell where it will be in ROM download mode")
    want = rom_port_path(":".join(serial[i:i + 2] for i in range(0, 12, 2)))
    # The connection itself resets the chip into ROM; this run fails as the port vanishes.
    tool([esptool, "--chip", chip, "--port", port, "--after", "no-reset", "chip-id"], 60)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if os.path.exists(want):
            time.sleep(1)
            break
        time.sleep(0.5)
    else:
        return None, (f"{name}: the stock app on {port} did not come back in ROM download mode "
                      f"({want} never appeared)")
    rc, out = tool(["python3", devreg, "guard", name, want], 30)
    if rc != 0:
        return None, f"devreg guard refuses {want} for {name} (its ROM download face): {out.strip()[-160:]}"
    return want, None


def image_field_from_bytes(imginfo, file, args):
    """A number the image carries in its own bytes (imginfo with `args`), or None if it
    cannot be read."""
    rc, out = tool(["python3", imginfo, file, *args], 30)
    try:
        return int(out.strip().splitlines()[-1]) if rc == 0 else None
    except (ValueError, IndexError):
        return None

def no_full_record(facts_chip):
    """Whether the survey left no full record of the chip's regions (its inventory): then a spot
    check has nothing to stand beside, and the read is full (docs/ARCHITECTURE.md#checking-depth)."""
    return not ((facts_chip or {}).get("partitions") or {}).get("inventory")
