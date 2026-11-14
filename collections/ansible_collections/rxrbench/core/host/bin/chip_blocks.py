#!/usr/bin/env python3
"""A whole ESP32 flash chip, block by block, in one ROM-loader session.

Reading a chip over the USB-Serial-JTAG runs at ~88 KB/s whatever the baud (16 MB in ~190 s),
and most of a chip is erased. The loader's stub hashes flash itself, fast, so:

    chip_blocks.py <port> info              {mac, flash_size, size}
    chip_blocks.py <port> read <out.bin>    hash every block, read only the ones that are not
                                            erased, write the whole image (erased blocks as
                                            0xFF, which is what they hold), each block checked
                                            against the chip's own hash
    chip_blocks.py <port> write <in.bin>    hash every block, write only the ones that differ
                                            from the image, hash those again
    chip_blocks.py <port> diff <in.bin>     hash every block and count the ones that differ from
                                            the image: what `write` would write; nothing is
                                            written (a restore's or a switch's plan)
    chip_blocks.py <port> place <in.bin> [--mac M]
                                            a merged image linked for 0x0, shorter than the
                                            chip: the chip becomes the image and 0xFF after it
                                            (the same block write). With --mac, a chip whose
                                            MAC is another is refused before anything is written
    chip_blocks.py <port> erase --mac M     the whole chip erased (the loader's erase_flash), in
                                            the session that read the chip's MAC: refused unless
                                            it is M (required). Every block is hashed after and
                                            must read erased
    chip_blocks.py <port> layout [<lo:hi>,...]
                                            what the write rule compares (rxrbench
                                            module_utils/regions.py): the partition table at
                                            0x8000, the otadata partition, and the first 64
                                            bytes of every app partition, as hex; nothing is
                                            written. With ranges (a spot check), the heads of
                                            only the app partitions they overlap. With
                                            --at <lo>,... and no table on the chip (an MCUboot
                                            layout), the heads at those offsets
    chip_blocks.py <port> match <in.bin> <lo:hi>... [--mac M]
                                            for each region (hex offsets), whether the chip's
                                            stub hash equals the file's bytes there, and the
                                            chip's otadata partition (which app it boots), as hex,
                                            or null with no table or no otadata; nothing is
                                            written. With --mac the answer says if it is that chip

    chip_blocks.py <port> region-read <in.bin> <lo:hi>=<out.bin>... --runs <lo:hi,...> [--mac M]
    chip_blocks.py <port> region-write <in.bin> <lo:hi>=<data.bin>... --runs <lo:hi,...> [--mac M]
    chip_blocks.py <port> region-erase <in.bin> <lo:hi>... --runs <lo:hi,...> [--mac M]
    chip_blocks.py <port> region-update <in.bin> <lo:hi>... --runs <lo:hi,...> [--mac M]
                                            one region of a chip that runs a merged image (a
                                            config region), in the same session as the
                                            check that it runs it: every --runs region must
                                            hash as the image's, and with --mac the chip must
                                            be that one, or nothing is read or written. The
                                            region is checked by the stub's hash after.
                                            region-update writes the image's own bytes there
                                            (an update: the app partitions of a chip on the
                                            image's partition table, nothing else)

Each prints one JSON object. The board is left in its loader: the caller boots it with a fresh
esptool session (`esptool chip-id`), because a reset from this session does not reliably boot
an S3 on the USB-Serial-JTAG (rxrbench module_utils/flash/chip.py).

Runs with the bench host's esptool venv (~/.venv-esp/bin/python).
"""
import hashlib
import json
import os
import struct
import sys
import time

BLOCK = 64 * 1024
ERASED = hashlib.md5(b"\xff" * BLOCK).hexdigest()


#: The baud the stub is moved to behind a UART bridge (a CH340, a CP210x): the ROM connects at
#: 115200, ~11 KB/s, so a nearly full 16 MB chip took longer than 15 min where 921600 reads it in
#: about 5. A native USB port (USB-Serial-JTAG, USB-OTG) runs at its own speed whatever the baud,
#: and is left as it is. RXRBENCH_ESP_BAUD overrides it (115200 keeps the ROM's).
FAST_BAUD = 921600


def native_usb(esp):
    """Whether the loader talks over the chip's own USB, where the baud means nothing."""
    for way in ("uses_usb_jtag_serial", "uses_usb_otg"):
        try:
            if getattr(esp, way, None) and getattr(esp, way)():
                return True
        except Exception:  # noqa: BLE001 - a chip that cannot say: treat it as a bridge
            pass
    return False


def connect(port, environ=None):
    from esptool import cmds  # pylint: disable=import-outside-toplevel
    esp = cmds.detect_chip(port, connect_mode="default-reset")
    esp = esp.run_stub()
    baud = int((environ if environ is not None else os.environ).get("RXRBENCH_ESP_BAUD") or FAST_BAUD)
    if baud != esp.ESP_ROM_BAUD and not native_usb(esp):
        esp.change_baud(baud)
    return esp


def size_of(esp):
    return 1 << ((esp.flash_id() >> 16) & 0xFF)


def hashes(esp, size):
    return [esp.flash_md5sum(off, BLOCK) for off in range(0, size, BLOCK)]


def runs(blocks):
    """Contiguous runs of block numbers, as (first, count)."""
    out = []
    for b in blocks:
        if out and out[-1][0] + out[-1][1] == b:
            out[-1][1] += 1
        else:
            out.append([b, 1])
    return out


def info(esp):
    return {"mac": ":".join(f"{b:02x}" for b in esp.read_mac()), "size": size_of(esp),
            "chip": esp.get_chip_description()}


def read(esp, path):
    t0 = time.monotonic()
    size = size_of(esp)
    sums = hashes(esp, size)
    used = [i for i, s in enumerate(sums) if s != ERASED]
    image = bytearray(b"\xff" * size)
    for first, count in runs(used):
        off = first * BLOCK
        image[off:off + count * BLOCK] = esp.read_flash(off, count * BLOCK)
    # Every block is checked against the chip's own hash: a block read is what the chip hashed,
    # and an erased block is erased by its hash. (A second, whole-chip hash would cost as much
    # again: the stub hashes ~600 KB/s.)
    for i in used:
        if hashlib.md5(image[i * BLOCK:(i + 1) * BLOCK]).hexdigest() != sums[i]:
            raise SystemExit(json.dumps({"ok": False, "error": f"block {i} read back wrong"}))
    with open(path, "wb") as f:
        f.write(image)
    return {"ok": True, "size": size, "blocks": len(sums), "read_blocks": len(used),
            "seconds": round(time.monotonic() - t0, 1)}


def _differ(esp, path, image):
    size = size_of(esp)
    if image is None:
        with open(path, "rb") as f:
            image = f.read()
    if len(image) != size:
        raise SystemExit(json.dumps({"ok": False, "error": f"the image is {len(image)} B, the "
                                                           f"chip {size} B"}))
    sums = hashes(esp, size)
    return image, size, sums, [i for i, s in enumerate(sums)
                               if s != hashlib.md5(image[i * BLOCK:(i + 1) * BLOCK]).hexdigest()]


def diff(esp, path, image=None):
    """What `write` would write: the blocks of the chip that differ from the image."""
    t0 = time.monotonic()
    _image, size, sums, differ = _differ(esp, path, image)
    return {"ok": True, "size": size, "blocks": len(sums), "differing_blocks": len(differ),
            "seconds": round(time.monotonic() - t0, 1)}


def write(esp, path, image=None, write_flash=None):
    t0 = time.monotonic()
    image, size, sums, differ = _differ(esp, path, image)
    if differ:
        if write_flash is None:
            from esptool import cmds  # pylint: disable=import-outside-toplevel
            write_flash = cmds.write_flash
        write_flash(esp, [(first * BLOCK, image[first * BLOCK:(first + count) * BLOCK])
                               for first, count in runs(differ)],
                         flash_freq="keep", flash_mode="keep", flash_size="keep")
    # The blocks that were the same were hashed equal above; the ones written are hashed now.
    for i in differ:
        if esp.flash_md5sum(i * BLOCK, BLOCK) != hashlib.md5(image[i * BLOCK:(i + 1) * BLOCK]
                                                             ).hexdigest():
            raise SystemExit(json.dumps({"ok": False, "error": f"block {i} does not match the "
                                                               f"image after the write"}))
    return {"ok": True, "size": size, "blocks": len(sums), "written_blocks": len(differ),
            "seconds": round(time.monotonic() - t0, 1)}


def erase(esp):
    """The whole chip erased, then every block hashed: each must hash as erased."""
    t0 = time.monotonic()
    esp.erase_flash()
    size = size_of(esp)
    sums = hashes(esp, size)
    left = [i for i, s in enumerate(sums) if s != ERASED]
    if left:
        raise SystemExit(json.dumps({"ok": False, "error": f"{len(left)} of {len(sums)} blocks do "
                                                           f"not read erased after the erase"}))
    return {"ok": True, "size": size, "blocks": len(sums), "erased": True,
            "seconds": round(time.monotonic() - t0, 1)}


def padded(path, size):
    """A merged image (linked for 0x0) as the whole chip it makes: its bytes, then 0xFF."""
    with open(path, "rb") as f:
        image = f.read()
    if len(image) > size:
        raise SystemExit(json.dumps({"ok": False, "error": f"the image is {len(image)} B, "
                                                           f"the chip only {size} B"}))
    return image + b"\xff" * (size - len(image))


def mac_refusal(chip, mac):
    """None, or why this is not the chip `mac` names (compared without case or colons)."""
    norm = lambda m: (m or "").lower().replace(":", "").replace("-", "")  # noqa: E731
    if mac and norm(chip["mac"]) != norm(mac):
        return f"the chip on this port has MAC {chip['mac']}, the board's is {mac}: not this board"
    return None


def place(esp, path, write_flash=None):
    return write(esp, path, image=padded(path, size_of(esp)), write_flash=write_flash)


def match(esp, path, regions, boot=True):
    """[{lo, hi, same}] for each (lo, hi): the chip's hash there against the file's bytes, and
    with `boot` the chip's otadata (`otadata_of`), so the caller knows which app it runs."""
    with open(path, "rb") as f:
        image = f.read()
    out = []
    for lo, hi in regions:
        if hi > len(image) or lo >= hi:
            raise SystemExit(json.dumps({"ok": False, "error": f"region {lo:#x}:{hi:#x} is not "
                                                               f"inside the image"}))
        want = hashlib.md5(image[lo:hi]).hexdigest()
        out.append({"lo": f"{lo:#x}", "hi": f"{hi:#x}", "same": esp.flash_md5sum(lo, hi - lo) == want})
    got = {"ok": True, "regions": out}
    if boot:
        got["otadata"] = otadata_of(esp, esp.read_flash(TABLE_AT, TABLE_LEN))
    return got


# The ESP-IDF partition table, as much of it as `layout` needs (the full parser is rxrbench's
# module_utils/esppart.py; this script runs alone on the bench host).
TABLE_AT, TABLE_LEN, ROW, HEAD = 0x8000, 0xC00, 32, 64


def otadata_of(esp, table):
    """The otadata partition's bytes (up to 0x2000) as hex, or None: no table, or no otadata."""
    if table[:2] != b"\xaa\x50":
        return None
    for pos in range(0, TABLE_LEN, ROW):
        row = table[pos:pos + ROW]
        if row[:2] != b"\xaa\x50":
            break
        off, size = struct.unpack_from("<II", row, 4)
        if row[2] == 1 and row[3] == 0:
            return esp.read_flash(off, min(size, 0x2000)).hex()
    return None


def layout(esp, only=None, at=None):
    """The table, otadata and every app partition's first HEAD bytes, read in this session;
    with `only` ([(lo, hi)]), the heads of the app partitions those ranges overlap alone; with
    `at` ([offset]) and no table on the chip, the heads at those offsets (an MCUboot layout's
    slots, from the image's map)."""
    table = esp.read_flash(TABLE_AT, TABLE_LEN)
    out = {"table": table.hex(), "otadata": otadata_of(esp, table), "heads": {}}
    if table[:2] != b"\xaa\x50":
        for off in at or []:
            out["heads"][f"{off:#x}"] = esp.read_flash(off, HEAD).hex()
        return out
    for pos in range(0, TABLE_LEN, ROW):
        row = table[pos:pos + ROW]
        if row[:2] != b"\xaa\x50":
            break
        off, size = struct.unpack_from("<II", row, 4)
        if row[2] == 0 and (only is None or any(lo < off + size and off < hi for lo, hi in only)):
            out["heads"][f"{off:#x}"] = esp.read_flash(off, HEAD).hex()
    return out


def span(text):
    lo, hi = (int(x, 16) for x in text.split(":"))
    return lo, hi


def region(esp, cmd, path, parts, runs, write_flash=None):
    """Config regions of a chip that runs the merged image at `path` (the docstring):
    `parts` [((lo, hi), file or None)], all in this one session."""
    got = match(esp, path, runs, boot=False)["regions"]
    differ = [f"{r['lo']}:{r['hi']}" for r in got if not r["same"]]
    if differ:
        return {"ok": False, "runs": False, "error": "the chip does not run the image ("
                f"{', '.join(differ)} differ), so its partition table may not be the image's"}
    done = []
    for (lo, hi), data in parts:
        size = hi - lo
        if cmd == "region-read":
            blob = esp.read_flash(lo, size)
            if hashlib.md5(blob).hexdigest() != esp.flash_md5sum(lo, size):
                return {"ok": False, "error": f"{lo:#x}:{hi:#x} read back wrong"}
            with open(data, "wb") as f:
                f.write(blob)
        else:
            if cmd == "region-erase":
                esp.erase_region(lo, size)
                want = hashlib.md5(b"\xff" * size).hexdigest()
            else:
                if cmd == "region-update":
                    with open(path, "rb") as f:
                        blob = f.read()[lo:hi]
                else:
                    with open(data, "rb") as f:
                        blob = f.read()
                if len(blob) != size:
                    return {"ok": False, "error": f"{data} is {len(blob)} B, {lo:#x}:{hi:#x} is "
                                                  f"{size} B"}
                if write_flash is None:
                    from esptool import cmds  # pylint: disable=import-outside-toplevel
                    write_flash = cmds.write_flash
                write_flash(esp, [(lo, blob)], flash_freq="keep", flash_mode="keep",
                            flash_size="keep")
                want = hashlib.md5(blob).hexdigest()
            if esp.flash_md5sum(lo, size) != want:
                return {"ok": False, "error": f"after the {cmd[7:]}, {lo:#x}:{hi:#x} does not "
                                              f"hash as it should"}
        done.append({"lo": f"{lo:#x}", "hi": f"{hi:#x}"})
    return {"ok": True, "runs": True, "regions": done}


def parts_of(cmd, args):
    """[((lo, hi), file)] from `lo:hi=file` arguments (`lo:hi` for an erase or an update)."""
    out = []
    for a in args:
        where, _eq, file = a.partition("=")
        if (cmd in ("region-erase", "region-update")) == bool(file):
            raise SystemExit(__doc__)
        out.append((span(where), file or None))
    return out


REGION_CMDS = ("region-read", "region-write", "region-erase", "region-update")
#: Every command this copy has. A caller newer than this copy asks for one it lacks: the answer
#: says so as JSON (`unknown_command`), so the caller can say the bench host's copy is out of
#: date (`just hosts-setup`) instead of showing this usage text.
COMMANDS = ("info", "read", "write", "diff", "place", "match", "layout", "erase", *REGION_CMDS)


def main(argv):
    mac = None
    at = None
    if "--at" in argv:
        i = argv.index("--at")
        try:
            at = [int(x, 16) for x in argv[i + 1].split(",") if x]
        except (IndexError, ValueError):
            raise SystemExit(__doc__) from None
        argv = argv[:i] + argv[i + 2:]
    if "--mac" in argv:
        i = argv.index("--mac")
        mac, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    runs = None
    if "--runs" in argv:
        i = argv.index("--runs")
        runs, argv = [span(r) for r in argv[i + 1].split(",") if r], argv[:i] + argv[i + 2:]
    cmd = argv[1] if len(argv) > 1 else None
    if cmd is not None and cmd not in COMMANDS:
        raise SystemExit(json.dumps({"ok": False, "unknown_command": cmd, "commands": list(COMMANDS),
                                     "error": f"this chip_blocks.py has no command {cmd!r}"}))
    if cmd not in COMMANDS or \
            (cmd in ("info", "erase") and len(argv) != 2) or \
            (cmd == "layout" and len(argv) not in (2, 3)) or \
            (cmd in ("read", "write", "diff", "place") and len(argv) != 3) \
            or (cmd == "match" and len(argv) < 4) \
            or (cmd in REGION_CMDS and (len(argv) < 4 or not runs)):
        raise SystemExit(__doc__)
    if cmd == "erase" and not mac:
        # nothing but the MAC read in this very session says which chip is on the port
        raise SystemExit(json.dumps({"ok": False, "error": "erase needs --mac: the chip's MAC is "
                                                           "the only proof of which chip it is"}))
    parts = parts_of(cmd, argv[3:]) if cmd in REGION_CMDS else None
    only = None
    if cmd == "layout" and len(argv) == 3:
        try:
            only = [span(r) for r in argv[2].split(",") if r]
        except ValueError:
            raise SystemExit(__doc__) from None
    esp = connect(argv[0])
    out = info(esp)
    why = mac_refusal(out, mac)
    if why and cmd in ("write", "place", "erase", *REGION_CMDS):
        raise SystemExit(json.dumps({**out, "ok": False, "error": why}))
    if cmd in REGION_CMDS:
        out.update(region(esp, cmd, argv[2], parts, runs))
        print(json.dumps(out))
        if out.get("ok") is False:
            raise SystemExit(1)
        return
    if mac:
        out["mac_ok"] = why is None
    if cmd == "layout":
        out.update(layout(esp, only, at))
    elif cmd == "read":
        out.update(read(esp, argv[2]))
    elif cmd == "write":
        out.update(write(esp, argv[2]))
    elif cmd == "diff":
        out.update(diff(esp, argv[2]))
    elif cmd == "erase":
        out.update(erase(esp))
    elif cmd == "place":
        out.update(place(esp, argv[2]))
    elif cmd == "match":
        out.update(match(esp, argv[2], [tuple(int(x, 16) for x in r.split(":")) for r in argv[3:]]))
    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv[1:])
