"""A board's flash as regions, each with an owner and the evidence for it, and the one rule a
write must pass (docs/ARCHITECTURE.md#partitions-and-the-write-rule).

Safety comes from comparing, not recognising: core never asks whose firmware a region holds,
only whether it holds an image and whether the write would destroy one the layout does not
declare. The layout of the flash is data with one owner (a partition table read from the chip,
the image's own flash map, ranges a catalog declares); what an image looks like is a table of
recognisers, which a plugin extends the way it extends the survey's USB rows.

A region: {start, size, label, kind, owner, evidence}
    kind      "app" (may hold an image), "data", "boot" (the bootloader's space), "table" (the
              partition table itself)
    owner     "declared" (the firmware the chip runs: the layout's to replace), "foreign"
              (holds an image of other firmware), "shared" (a data region on a chip with a
              manager: every app keeps data there), "empty" (erased), "unknown" (not read)
    evidence  {how: "table" | "header" | "declared" | "not read", recogniser, depth}
"""
import struct

#: How many bytes of a region's start a recogniser may look at: one short read per region.
HEADER = 64

#: Recognisers: what makes the start of a region an image, as data. `magic` is hex at `at`;
#: `u32le` optionally bounds a little-endian word (an entry address) at its offset. A
#: recogniser says an image is present, never whose it is.
CORE_RECOGNISERS = (
    # An ESP32 application image: magic 0xE9, then an entry point in the chip's address space.
    {"name": "esp-app-image", "at": 0, "magic": "e9",
     "u32le": {"at": 4, "min": 0x40000000, "max": 0x50000000}},
    # An MCUboot image: its header's magic, little-endian 0x96f3b83d.
    {"name": "mcuboot-image", "at": 0, "magic": "3db8f396"},
)
_RECOGNISERS = list(CORE_RECOGNISERS)


def register_recognisers(rows):
    """Core's recognisers, then the loaded plugins' (a list of the same rows)."""
    _RECOGNISERS[:] = list(CORE_RECOGNISERS) + [dict(r) for r in (rows or ())]


def recognise(head):
    """The name of the first recogniser that matches these bytes, or None. All 0xFF is None
    too: an erased region holds nothing."""
    for r in _RECOGNISERS:
        magic = bytes.fromhex(r["magic"])
        if head[r["at"]:r["at"] + len(magic)] != magic:
            continue
        bound = r.get("u32le")
        if bound:
            if len(head) < bound["at"] + 4:
                continue
            word = struct.unpack_from("<I", head, bound["at"])[0]
            if not bound["min"] <= word < bound["max"]:
                continue
        return r["name"]
    return None


def erased(head):
    return bool(head) and all(b == 0xFF for b in head)


def from_esp_table(rows, table_at=0x8000, table_size=0xC00):
    """Regions from an ESP-IDF partition table's rows (esppart.parse): the bootloader's space
    before the table, the table itself, then each row."""
    out = [{"start": 0, "size": table_at, "label": "bootloader", "kind": "boot"},
           {"start": table_at, "size": table_size, "label": "partition table", "kind": "table"}]
    for r in rows:
        out.append({"start": r["offset"], "size": r["size"], "label": r["label"],
                    "kind": "app" if r["type"] == 0 else "data"})
    return out


KINDS = ("boot", "app", "data")
ROLES = ("config",)


def declared(rows):
    """(regions, problems) from ranges a catalog image or a family declares for a chip with no
    layout of its own (docs/ARCHITECTURE.md#partitions-and-the-write-rule): [{label, kind, start, size, role?}],
    `start` and `size` integers or their hex text, addresses absolute. Refused: an unknown kind
    or role, a `config` role on anything but a data region, a size that is not positive,
    two ranges that overlap, two that share a label."""
    out, problems = [], []
    for i, row in enumerate(rows or ()):
        row = row or {}
        label = str(row.get("label") or f"range {i}")
        try:
            start, size = int(str(row.get("start")), 0), int(str(row.get("size")), 0)
        except (TypeError, ValueError):
            problems.append(f"{label}: start and size must be numbers (0x...)")
            continue
        kind, role = row.get("kind"), row.get("role")
        if kind not in KINDS:
            problems.append(f"{label}: kind {kind!r} is none of {', '.join(KINDS)}")
        if role is not None and role not in ROLES:
            problems.append(f"{label}: role {role!r} is none of {', '.join(ROLES)}")
        if role == "config" and kind != "data":
            problems.append(f"{label}: a config role is on a data region, not a {kind} one")
        if size <= 0:
            problems.append(f"{label}: size {size} is not positive")
        out.append({"start": start, "size": size, "label": label, "kind": kind,
                    **({"role": role} if role else {})})
    for a, b in zip(sorted(out, key=lambda r: r["start"]), sorted(out, key=lambda r: r["start"])[1:]):
        if a["start"] + a["size"] > b["start"]:
            problems.append(f"{a['label']} and {b['label']} overlap at {b['start']:#x}")
    labels = [r["label"] for r in out]
    problems += [f"two ranges are labelled {x}" for x in sorted({x for x in labels if labels.count(x) > 1})]
    return out, problems


def config_ranges(ranges):
    """[(label, start, size)] of the declared ranges whose role is `config`: the config regions
    of a chip with no table of its own, at absolute addresses (docs/ARCHITECTURE.md#chip-and-region-flows).
    `ranges` as `declared` returns them, or as declared (hex text)."""
    return [(str(r["label"]), int(str(r["start"]), 0), int(str(r["size"]), 0))
            for r in ranges or () if r.get("role") == "config"]


def overlaps(region, ranges):
    a0, a1 = region["start"], region["start"] + region["size"]
    return any(s < a1 and a0 < s + n for s, n in ranges)


def inventory(regions, read, declared, depth="full", writes=(), shared_data=False):
    """Each region with its owner and evidence.

    regions   the chip's layout (from_esp_table, or declared ranges)
    read      read(offset, length) -> bytes, the flasher's own read of the chip
    declared  [(start, size)] that the declared image brings: an image there is the layout's
    depth     "full": the header of every app region is read; "spot": only those the write
              overlaps (`writes`), the only ones a write can destroy (docs/ARCHITECTURE.md#checking-depth)
    writes    [(start, size)] the write would cover
    shared_data  the chip has a manager: its data regions are every app's (docs/
              ARCHITECTURE.md#partitions-and-the-write-rule); else they are the firmware's
    """
    out = []
    for reg in regions:
        reg = dict(reg)
        if reg["kind"] == "data":
            reg.update(owner="shared" if shared_data else "declared",
                       evidence={"how": "table", "shared": bool(shared_data)})
        elif reg["kind"] != "app":
            reg.update(owner="unknown", evidence={"how": "not read"})
        elif depth == "spot" and not overlaps(reg, writes):
            reg.update(owner="unknown", evidence={"how": "not read", "depth": "spot"})
        else:
            head = read(reg["start"], HEADER)
            name = recognise(head)
            if name is None:
                owner = "empty" if erased(head) else "unknown"
            else:
                owner = "declared" if overlaps(reg, declared) else "foreign"
            reg.update(owner=owner, evidence={"how": "header", "recogniser": name, "depth": depth})
        out.append(reg)
    return out


def dropped(old, new):
    """[(start, size)] of the old layout's app and data regions the new one does not keep (same
    start, size and kind): a new partition table that drops a region orphans what it holds,
    as surely as a write over it."""
    keep = {(r["start"], r["size"], r["kind"]) for r in new}
    return [(r["start"], r["size"]) for r in old
            if r["kind"] in ("app", "data") and (r["start"], r["size"], r["kind"]) not in keep]


def refusals(inv, writes, allowed=(), drops=(), protect=()):
    """Why a write must not run, given the inventory: [] when it may.

    writes   [(start, size)] the write covers
    allowed  labels of foreign regions the layout says the write may take, or ("all",)
    drops    [(start, size)] of regions a new partition table drops (`dropped`)
    protect  kinds of region a write may not cover unless `allowed` names it: ("boot",) on a
             chip whose bootloader no other step writes (declared ranges)
    """
    out = []
    take_all = "all" in allowed
    for reg in inv:
        if reg["kind"] in protect and not take_all and reg["label"] not in allowed \
                and overlaps(reg, writes):
            out.append(f"the write covers {reg['label']} at {reg['start']:#x}, the {reg['kind']} "
                       "region the chip starts from; name it in bench_overwrite if that is meant")
    for reg in inv:
        if reg.get("owner") not in ("foreign", "shared") or take_all or reg["label"] in allowed:
            continue
        what = ("the write covers" if overlaps(reg, writes) else
                "the new partition table drops" if overlaps(reg, drops) else None)
        if not what:
            continue
        held = (f"an image the layout does not declare ({reg['evidence'].get('recogniser')})"
                if reg["owner"] == "foreign" else
                "data every app on this chip shares (it has an app manager)")
        out.append(f"{what} {reg['label']} at {reg['start']:#x}, which holds {held}; name it "
                   "in bench_overwrite if that is meant")
    if inv:
        known = [(r["start"], r["size"]) for r in inv]
        for s, n in writes:
            if not overlaps({"start": s, "size": n}, known):
                out.append(f"the write at {s:#x} ({n} bytes) lands outside every region the chip's "
                           "layout has")
    return out
