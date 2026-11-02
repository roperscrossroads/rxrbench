"""ESP-IDF partition tables for tests: rows as bytes, a table as a 0xC00 blob, and the table
of a chip with an app manager (nvs, otadata, the manager's own `test` partition)."""


def row(ptype, subtype, offset, size, label, flags=0):
    return (b"\xaa\x50" + bytes([ptype, subtype]) + offset.to_bytes(4, "little")
            + size.to_bytes(4, "little") + label.encode().ljust(16, b"\0")
            + flags.to_bytes(4, "little"))


def table(rows, length=0xC00):
    data = b"".join(rows)
    return data + b"\xff" * (length - len(data)) if length else data


#: A chip with an app manager in a `test` partition: core's tests know it by that shape alone
#: (esppart.manager), never by sizes.
MANAGED_4MB = [row(1, 0x02, 0x9000, 0x5000, "nvs"), row(1, 0x00, 0xE000, 0x2000, "otadata"),
               row(0, 0x20, 0x10000, 0x150000, "test")]
#: The table Launcher ships on a 4 MB chip: the launcher plugin's tests.
LAUNCHER_4MB = MANAGED_4MB


def flash_with_table(rows, size=0x10000):
    """The first `size` bytes of a chip: 0xFF with the table at 0x8000 (what a 64 KB read of
    the bootloader region holds)."""
    data = bytearray(b"\xff" * size)
    t = table(rows)
    data[0x8000:0x8000 + len(t)] = t
    return bytes(data)


#: The start of an ESP32 application image: magic 0xE9, then an entry point in the chip's space.
APP_HEADER = b"\xe9\x04\x02\x20" + (0x40080000).to_bytes(4, "little") + b"\0" * 56


def otadata(seq):
    """An otadata partition whose first entry selects ota_((seq - 1) % n), as ESP-IDF writes it."""
    import binascii
    import struct
    blob = bytearray(b"\xff" * 0x2000)
    crc = binascii.crc32(struct.pack("<I", seq), 0xFFFFFFFF) & 0xFFFFFFFF
    blob[0:32] = struct.pack("<I", seq) + b"\xff" * 20 + struct.pack("<II", 2, crc)
    return bytes(blob)


def chip(rows, images=(), boot_seq=None, otadata_at=0xE000):
    """A whole chip's bytes: the table at 0x8000, an image header at each offset of `images`,
    otadata selecting `boot_seq` (None: erased), 0xFF elsewhere."""
    size = max([0x10000] + [r_off + 0x1000 for r_off in images])
    data = bytearray(b"\xff" * size)
    t = table(rows)
    data[0x8000:0x8000 + len(t)] = t
    for off in images:
        data[off:off + len(APP_HEADER)] = APP_HEADER
    if boot_seq is not None:
        data[otadata_at:otadata_at + 0x2000] = otadata(boot_seq)
    return bytes(data)


def recorded(rows, images=(), boot_seq=None, gathered="2026-11-07T18:00:00Z"):
    """The survey's esptool record of the chip `chip(rows, images, boot_seq)` describes:
    {partitions: {manager, boot, inventory}, gathered}, as `just survey` writes it in the facts."""
    import importlib.util
    import os
    from ansible_collections.rxrbench.core.plugins.module_utils import esppart
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location("usb_survey", os.path.join(
        repo, "collections", "ansible_collections", "rxrbench", "core", "plugins", "modules",
        "usb_survey.py"))
    survey = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(survey)
    blob = chip(rows, images=images, boot_seq=boot_seq)
    parsed = esppart.parse(blob[0x8000:0x8C00])
    rec = survey.survey_inventory(parsed, lambda o, n: blob[o:o + n])
    return {"partitions": {"manager": esppart.manager(parsed), **rec}, "gathered": gathered}
