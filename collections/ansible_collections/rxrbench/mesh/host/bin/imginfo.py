#!/usr/bin/env python3
"""What is this signed MCUboot image? Version, size, fleet class, hash: from the
header and the protected TLV block, the way the firmware's own image depot reads
it. The image is the manifest; the filename is for humans.

Usage: imginfo.py <image.bin|merged.hex|dfu.zip> [--json] [--name] [--class]
  A .hex (or the .hex inside an adafruit-nrfutil DFU .zip, what a XIAO is flashed with)
  holds MCUboot and the app; the app is the first MCUboot image header in it.
  --json   machine-readable
  --name   just the filename a firmware depot keeps it under: <class>-<maj.min.rev>.bin
  --class  just the fleet class number (0 = unclassed); what the flash scripts check
Exit 0 on a signed image, 2 if the file is not one, 3 if --name and the image is
unclassed (class TLV 0x00A0 missing: a depot would never offer it).
"""
import io
import json
import struct
import sys
import zipfile

MAGIC = 0x96F3B83D
TLV_INFO_MAGIC = 0x6907
TLV_PROT_INFO_MAGIC = 0x6908
TLV_SHA256 = 0x10
TLV_FLEET_CLASS = 0x00A0


def _hex_to_bytes(text: str) -> bytes:
    """Intel HEX -> one contiguous image from the lowest address (gaps 0xff)."""
    chunks, base = {}, 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(":"):
            continue
        rec = bytes.fromhex(line[1:])
        n, addr, typ = rec[0], (rec[1] << 8) | rec[2], rec[3]
        if typ == 0:
            chunks[base + addr] = rec[4:4 + n]
        elif typ == 4:
            base = ((rec[4] << 8) | rec[5]) << 16
        elif typ == 2:
            base = ((rec[4] << 8) | rec[5]) << 4
        elif typ == 1:
            break
    if not chunks:
        raise ValueError("empty hex")
    lo = min(chunks)
    hi = max(a + len(d) for a, d in chunks.items())
    buf = bytearray(b"\xff" * (hi - lo))
    for a, d in chunks.items():
        buf[a - lo:a - lo + len(d)] = d
    return bytes(buf)


def _load(path: str) -> bytes:
    """The signed app image, from a signed .bin, a merged .hex, or a DFU .zip."""
    if path.endswith(".zip"):
        # adafruit-nrfutil genpkg stores the hex converted to one .bin (from its lowest
        # address, i.e. MCUboot first), beside a .dat and manifest.json.
        with zipfile.ZipFile(path) as z:
            bins = [n for n in z.namelist() if n.endswith(".bin")]
            hexes = [n for n in z.namelist() if n.endswith(".hex")]
            if bins:
                blob = z.read(bins[0])
            elif hexes:
                blob = _hex_to_bytes(z.read(hexes[0]).decode("ascii"))
            else:
                raise ValueError("zip holds no .bin or .hex")
    elif path.endswith(".hex"):
        with open(path) as f:
            blob = _hex_to_bytes(f.read())
    else:
        with open(path, "rb") as f:
            return f.read()
    # MCUboot carries no image header of its own, but its code contains the magic as a
    # constant, so a bare search finds that first. A real header is one whose TLV block
    # sits exactly at hdr_size + img_size.
    magic = struct.pack("<I", MAGIC)
    at = 0
    while True:
        at = blob.find(magic, at)
        if at < 0:
            raise ValueError("no MCUboot image header in the image")
        if at % 4 == 0 and at + 32 <= len(blob):
            _, _, hdr_size, _, img_size = struct.unpack_from("<IIHHI", blob, at)
            tlv = at + hdr_size + img_size
            if 32 <= hdr_size <= 0x1000 and tlv + 4 <= len(blob):
                m, = struct.unpack_from("<H", blob, tlv)
                if m in (TLV_INFO_MAGIC, TLV_PROT_INFO_MAGIC):
                    return blob[at:]
        at += 1


def inspect(path: str) -> dict:
    data = _load(path)
    if len(data) < 32:
        raise ValueError("too short")
    (magic, load_addr, hdr_size, prot_tlv_size, img_size, flags,
     major, minor, rev, build) = struct.unpack_from("<IIHHIIBBHI", data, 0)
    if magic != MAGIC:
        raise ValueError("not an MCUboot image (bad magic)")
    info = {
        "file": path, "version": f"{major}.{minor}.{rev}", "build": build,
        "packed_version": (major << 24) | (minor << 16) | rev,
        "hdr_size": hdr_size, "img_size": img_size, "class": 0, "sha256": None,
        "tlvs": {},
    }
    off = hdr_size + img_size

    def walk(start, end, protected):
        p = start
        while p + 4 <= end:
            t, ln = struct.unpack_from("<HH", data, p)
            p += 4
            val = data[p:p + ln]
            p += ln
            info["tlvs"][f"0x{t:04x}" + ("p" if protected else "")] = val.hex()
            if protected and t == TLV_FLEET_CLASS and ln >= 1:
                info["class"] = val[0]
            if t == TLV_SHA256:
                info["sha256"] = val.hex()

    m, tot = struct.unpack_from("<HH", data, off)
    if m == TLV_PROT_INFO_MAGIC:
        walk(off + 4, off + tot, True)
        off += tot
        m, tot = struct.unpack_from("<HH", data, off)
    if m != TLV_INFO_MAGIC:
        raise ValueError("TLV block missing (unsigned?)")
    walk(off + 4, off + tot, False)
    info["size"] = off + tot          # what an update uploads
    info["name"] = f"{info['class']}-{info['version']}.bin"
    return info


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    try:
        info = inspect(args[0])
    except (OSError, ValueError, struct.error) as exc:
        print(f"{args[0]}: {exc}", file=sys.stderr)
        return 2
    if "--name" in sys.argv:
        if info["class"] == 0:
            print(f"{args[0]}: unclassed image (no TLV 0x00A0) — a depot would offer it to nobody",
                  file=sys.stderr)
            return 3
        print(info["name"])
        return 0
    if "--class" in sys.argv:
        print(info["class"])
        return 0
    if "--json" in sys.argv:
        print(json.dumps(info, indent=2))
        return 0
    cls = info["class"] if info["class"] else "-  (UNCLASSED: offered to nobody)"
    print(f"{args[0]}: {info['version']}+{info['build']}  class {cls}  {info['size']} B"
          f"  img-sha256(TLV) {info['sha256'][:8] if info['sha256'] else '?'}  -> {info['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
