"""Images built elsewhere, as the bench writes them: an Intel HEX or ELF file becomes one raw
binary and the flash address its first byte goes at (`base`).

    data, base, spans = imagefile.flatten(path)    # bytes, int, [(lo, hi), ...]

Core's flashers write raw binaries; a probe writes them at an address. A HEX or ELF file says
where each piece goes, often not at the start of flash (behind a bootloader, after a vector
table kept elsewhere), so staging (tools/stage_raw.py) converts it once: the pieces in address
order, the holes between them filled with 0xFF (what erased flash reads), and the lowest
address as `base` in the manifest (docs/ARCHITECTURE.md#core-portability).

Refused, with what to do: a file with no loadable bytes, pieces that overlap, and a piece outside
the chip's flash (a HEX that also writes a chip's configuration area, such as an nRF52's UICR
or an STM32's option bytes: the bench does not write those, so the file is staged without them).
Where the flash is comes from the board type (`flash_base` and `flash`, `flash_area`): with it,
pieces anywhere in flash make one image, however far apart (a bootloader at the start and an
app in its slot); without it, pieces more than MAX_GAP apart are taken for another area.

Standard library only.
"""
import struct

#: The widest hole between two pieces of one image when where the flash is is not known; a
#: wider one is taken for another memory area.
MAX_GAP = 64 * 1024
UNITS = {"": 1, "B": 1, "K": 1024, "KB": 1024, "M": 1024 ** 2, "MB": 1024 ** 2}


def size_of(text):
    """A size as a board type writes it (2MB, 512KB, 0x200000, 1048576) -> bytes."""
    t = str(text).strip().upper()
    if t.startswith("0X"):
        return int(t, 16)
    num = t.rstrip("KMB")
    unit = t[len(num):]
    if unit not in UNITS:
        raise ValueError(f"not a size: {text!r}")
    return int(num, 0) * UNITS[unit]


def flash_area(board_type):
    """(lo, hi) of a board type's flash, or None when it does not say both where and how much."""
    b = board_type or {}
    if b.get("flash_base") is None or b.get("flash") is None:
        return None
    lo = int(str(b["flash_base"]), 0)
    return lo, lo + size_of(b["flash"])


class ImageFileError(Exception):
    pass


def kind_of(path):
    """`ihex`, `elf` or None, from the file's own first bytes (never the name alone)."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head == b"\x7fELF":
        return "elf"
    if head[:1] == b":":
        return "ihex"
    return None


def ihex_pieces(path):
    """[(address, bytes)] of an Intel HEX file's data records, the checksum of each checked."""
    pieces, upper = [], 0
    with open(path, "r", encoding="ascii", errors="strict") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if not line.startswith(":"):
                raise ImageFileError(f"{path}:{n}: not an Intel HEX record")
            try:
                rec = bytes.fromhex(line[1:])
            except ValueError as e:
                raise ImageFileError(f"{path}:{n}: not hex ({e})") from e
            if len(rec) < 5 or len(rec) != rec[0] + 5:
                raise ImageFileError(f"{path}:{n}: record length does not match its count")
            if sum(rec) & 0xFF:
                raise ImageFileError(f"{path}:{n}: checksum does not match")
            count, addr, typ, data = rec[0], (rec[1] << 8) | rec[2], rec[3], rec[4:-1]
            if typ == 0:
                pieces.append((upper + addr, bytes(data[:count])))
            elif typ == 1:
                break
            elif typ == 2:
                upper = int.from_bytes(data, "big") << 4
            elif typ == 4:
                upper = int.from_bytes(data, "big") << 16
            elif typ in (3, 5):
                continue        # a start address: where it runs, not where it is written
            else:
                raise ImageFileError(f"{path}:{n}: record type {typ} is not Intel HEX")
    return pieces


def elf_pieces(path):
    """[(address, bytes)] of an ELF file's loadable segments, at their load (physical)
    addresses: what is written to flash, not where it runs (.data is copied to RAM at boot)."""
    with open(path, "rb") as f:
        blob = f.read()
    if blob[:4] != b"\x7fELF" or len(blob) < 52:
        raise ImageFileError(f"{path}: not an ELF file")
    cls, data = blob[4], blob[5]
    if cls not in (1, 2) or data not in (1, 2):
        raise ImageFileError(f"{path}: an ELF class or byte order this does not read")
    end = "<" if data == 1 else ">"
    if cls == 1:
        phoff, = struct.unpack_from(end + "I", blob, 28)
        phentsize, phnum = struct.unpack_from(end + "HH", blob, 42)
        fmt = end + "IIIIIIII"      # type, offset, vaddr, paddr, filesz, memsz, flags, align
    else:
        phoff, = struct.unpack_from(end + "Q", blob, 32)
        phentsize, phnum = struct.unpack_from(end + "HH", blob, 54)
        fmt = end + "IIQQQQQQ"      # type, flags, offset, vaddr, paddr, filesz, memsz, align
    pieces = []
    for i in range(phnum):
        off = phoff + i * phentsize
        if off + struct.calcsize(fmt) > len(blob):
            raise ImageFileError(f"{path}: program header {i} runs past the end of the file")
        h = struct.unpack_from(fmt, blob, off)
        if cls == 1:
            ptype, poff, _v, paddr, filesz = h[0], h[1], h[2], h[3], h[4]
        else:
            ptype, poff, _v, paddr, filesz = h[0], h[2], h[3], h[4], h[5]
        if ptype != 1 or filesz == 0:       # PT_LOAD with bytes in the file
            continue
        if poff + filesz > len(blob):
            raise ImageFileError(f"{path}: segment {i} runs past the end of the file")
        pieces.append((paddr, blob[poff:poff + filesz]))
    return pieces


def flatten(path, kind=None, max_gap=MAX_GAP, flash=None):
    """(data, base, spans): the file's pieces as one binary from its lowest address, holes
    filled with 0xFF; `spans` the (lo, hi) of each run of pieces, for the manifest. With
    `flash` ((lo, hi), `flash_area`) every piece must be inside it and the gaps do not matter."""
    kind = kind or kind_of(path)
    if kind == "ihex":
        pieces = ihex_pieces(path)
    elif kind == "elf":
        pieces = elf_pieces(path)
    else:
        raise ImageFileError(f"{path}: neither Intel HEX nor ELF")
    pieces = sorted((a, d) for a, d in pieces if d)
    if not pieces:
        raise ImageFileError(f"{path}: no bytes to write")
    if flash:
        outside = [(a, a + len(d)) for a, d in pieces if a < flash[0] or a + len(d) > flash[1]]
        if outside:
            raise ImageFileError(
                f"{path}: it writes outside the chip's flash ({flash[0]:#x}-{flash[1]:#x}): "
                + ", ".join(f"{lo:#x}-{hi:#x}" for lo, hi in outside)
                + ", a configuration area (an nRF52's UICR, an STM32's option bytes). The bench "
                "writes flash only: stage the flash part on its own (objcopy --remove-section)")
        max_gap = flash[1] - flash[0]
    spans = []
    for a, d in pieces:
        if spans and a < spans[-1][1]:
            raise ImageFileError(f"{path}: two pieces overlap at {a:#x}")
        if spans and a - spans[-1][1] <= max_gap:
            spans[-1][1] = a + len(d)
        else:
            spans.append([a, a + len(d)])
    if len(spans) > 1:
        raise ImageFileError(
            f"{path}: it writes {len(spans)} areas apart ("
            + ", ".join(f"{lo:#x}-{hi:#x}" for lo, hi in spans)
            + "), more than one image or a configuration area beside the flash (an nRF52's "
            "UICR, an STM32's option bytes). The bench writes flash only: stage the flash part "
            "on its own (objcopy --remove-section, or the build's own .bin with its address)")
    base = spans[0][0]
    out = bytearray(b"\xff" * (spans[0][1] - base))
    for a, d in pieces:
        out[a - base:a - base + len(d)] = d
    return bytes(out), base, [tuple(s) for s in spans]
