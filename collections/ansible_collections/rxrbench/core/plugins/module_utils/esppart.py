"""The ESP-IDF partition table, from its bytes: the same shape for a merged image file and for
what a chip's flash holds at 0x8000.

The table is up to 0xC00 bytes of 32-byte rows: magic `AA 50`, type, subtype, offset, size
(both little-endian 32-bit), a 16-byte label and 4 bytes of flags; an MD5 row (`EB EB`) or
an erased row (`FF FF`) ends it. A chip that carries no ESP-IDF table (an MCUboot layout has
MCUboot's own bytes or 0xFF there) parses to no rows, never to an error.

What this module recognises beyond the rows is an **app manager**, by structure, in ESP-IDF's
own terms: a factory or test app on a chip whose `otadata` partition boots other apps. The
installed apps are its `ota_N` rows. No one manager's labels, offsets or sizes are known here
(docs/ARCHITECTURE.md#partitions-and-the-write-rule); which writes would destroy what is `regions.py`'s to say, and
every ESP32 flasher asks before it writes (`flash/common.py` `esp_write_refusals`).
"""
import binascii
import struct

OFFSET = 0x8000
LENGTH = 0xC00
MAGIC = b"\xaa\x50"
ROW = 32

APP, DATA = 0x00, 0x01
SUB_FACTORY, SUB_TEST = 0x00, 0x20
SUB_OTA_FIRST, SUB_OTA_LAST = 0x10, 0x1F
SUB_NVS, SUB_OTADATA = 0x02, 0x00

def parse(blob, offset=0):
    """The rows of the table at `offset` in `blob`: [{type, subtype, offset, size, label,
    flags}]. No table there (no magic) is an empty list."""
    rows = []
    pos = offset
    while pos + ROW <= len(blob) and pos < offset + LENGTH:
        row = blob[pos:pos + ROW]
        if row[:2] != MAGIC:
            break
        ptype, sub = row[2], row[3]
        off, size, = struct.unpack_from("<II", row, 4)
        label = row[12:28].split(b"\0", 1)[0].decode("ascii", errors="replace")
        flags = struct.unpack_from("<I", row, 28)[0]
        rows.append({"type": ptype, "subtype": sub, "offset": off, "size": size,
                     "label": label, "flags": flags})
        pos += ROW
    return rows


def subtype_name(ptype, sub):
    if ptype == APP:
        if sub == SUB_FACTORY:
            return "factory"
        if sub == SUB_TEST:
            return "test"
        if SUB_OTA_FIRST <= sub <= SUB_OTA_LAST:
            return f"ota_{sub - SUB_OTA_FIRST}"
    elif ptype == DATA:
        return {0x00: "ota", 0x01: "phy", 0x02: "nvs", 0x03: "coredump", 0x04: "nvs_keys",
                0x05: "efuse", 0x81: "fat", 0x82: "spiffs", 0x83: "littlefs"}.get(sub)
    return None


def manager(rows):
    """The app manager these rows describe, or None: {label, offset, size, subtype, apps:
    [{label, subtype, offset, size}], data: [{label, subtype, offset, size}]}. A factory or test
    app on a chip with an otadata partition: the app that installs and boots the others.
    `apps` are its ota slots, in flash order, whether or not they hold an image yet."""
    if not rows or not any(r["type"] == DATA and r["subtype"] == SUB_OTADATA for r in rows):
        return None
    own = next((r for r in sorted(rows, key=lambda r: r["offset"])
                if r["type"] == APP and r["subtype"] in (SUB_TEST, SUB_FACTORY)), None)
    if own is None:
        return None
    apps = sorted((r for r in rows if r["type"] == APP and r is not own
                   and SUB_OTA_FIRST <= r["subtype"] <= SUB_OTA_LAST), key=lambda r: r["offset"])
    data = sorted((r for r in rows if r["type"] == DATA and r["offset"] > own["offset"]),
                  key=lambda r: r["offset"])
    slim = lambda r: {"label": r["label"], "subtype": subtype_name(r["type"], r["subtype"]),  # noqa: E731
                      "offset": r["offset"], "size": r["size"]}
    return {"label": own["label"], "offset": own["offset"], "size": own["size"],
            "subtype": subtype_name(APP, own["subtype"]), "apps": [slim(r) for r in apps],
            "data": [slim(r) for r in data]}


def describe(mgr):
    """One line for a refusal or a fact: what the chip carries."""
    if not mgr:
        return "no app manager"
    apps = ", ".join(a["label"] for a in mgr["apps"]) or "none installed"
    return f"an app manager in {mgr['label']} at {mgr['offset']:#x} with apps: {apps}"


# ---- which app the chip boots: ESP-IDF's otadata (two 32-byte entries, at 0 and 0x1000 of the
# `otadata` partition: ota_seq u32, seq_label[20], ota_state u32, crc u32 = CRC-32 of ota_seq
# seeded 0xFFFFFFFF, as ESP-IDF's otatool computes it). An entry counts when its seq is not
# erased, its CRC holds and its state is not INVALID (3) or ABORTED (4); the highest seq wins,
# and selects ota_((seq - 1) % number of ota slots).
OTADATA_ENTRY = 0x1000
OTA_STATE_DEAD = (3, 4)


def _ota_seq(entry):
    seq, = struct.unpack_from("<I", entry, 0)
    state, crc = struct.unpack_from("<II", entry, 24)
    if seq == 0xFFFFFFFF or crc != binascii.crc32(struct.pack("<I", seq), 0xFFFFFFFF) & 0xFFFFFFFF:
        return None
    return None if state in OTA_STATE_DEAD else seq


def boot_app(rows, otadata):
    """The app row the chip boots (`rows` from parse, `otadata` the otadata partition's bytes,
    or None when it was not read): the ota slot otadata selects, else the factory app, else
    the first app. None when the table has no app."""
    apps = [r for r in rows if r["type"] == APP]
    otas = sorted((r for r in apps if SUB_OTA_FIRST <= r["subtype"] <= SUB_OTA_LAST),
                  key=lambda r: r["subtype"])
    if otadata and otas:
        seqs = [q for q in (_ota_seq(otadata[i:i + 32]) for i in (0, OTADATA_ENTRY)
                            if len(otadata) >= i + 32) if q is not None]
        if seqs:
            return otas[(max(seqs) - 1) % len(otas)]
    factory = [r for r in apps if r["subtype"] == SUB_FACTORY]
    return (factory or apps or [None])[0]


def firmware_apps(rows, otadata):
    """The app rows that hold the firmware the chip runs: its boot app (`boot_app`), and, on a
    chip with no factory or test app (a plain A/B pair), the other ota slots, which hold that
    firmware's previous images. On a chip with a manager, the boot app alone: each other image
    is another firmware (docs/ARCHITECTURE.md#partitions-and-the-write-rule)."""
    boot = boot_app(rows, otadata)
    if boot is None:
        return []
    apps = [r for r in rows if r["type"] == APP]
    otas = [r for r in apps if SUB_OTA_FIRST <= r["subtype"] <= SUB_OTA_LAST]
    if boot in otas and not any(r["subtype"] in (SUB_FACTORY, SUB_TEST) for r in apps):
        return otas
    return [boot]


def inventory(rows, otadata, read, depth="full", writes=()):
    """The chip's regions with their owners (regions.inventory) from its table rows, its
    otadata and a read of the chip: the firmware it runs is declared, other images foreign,
    and on a chip with a manager every data region shared (docs/ARCHITECTURE.md#partitions-and-the-write-rule). Used
    by the write rule before a write and by the survey, which records it for the plan."""
    from . import regions  # pylint: disable=import-outside-toplevel
    owned = [(r["offset"], r["size"]) for r in firmware_apps(rows, otadata)]
    return regions.inventory(regions.from_esp_table(rows), read, declared=owned, depth=depth,
                             writes=writes, shared_data=manager(rows) is not None)

