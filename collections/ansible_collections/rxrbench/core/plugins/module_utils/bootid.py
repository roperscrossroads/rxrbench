"""What a bootloader is, from its bytes: the same shape for a staged file and for what a
device's flash holds.

For MCUboot the property that decides whether it will boot the bench's images is the **signing key
compiled into it**; its build only says how old it is. So a bootloader's identity is
`{kind, key, built, sha256, length}`:

- `key`: sha256 (first 16 hex) of the embedded P-256 public key (DER SubjectPublicKeyInfo),
  or None when there is none (no MCUboot: a stock image, an erased chip);
- `built`: MCUboot's own "compile time" string, when it carries one (the ESP32 port does);
- `sha256` / `length`: over exactly the bootloader image. On an ESP32 the length comes from
  the image header (segments, checksum, appended hash), so a 64 KB read of the partition
  hashes the same as the file that was written.

An older MCUboot build with the same key boots today's images (docs/HARDWARE.md,
"ESP32-S3": the board and the builds compared).
"""
import hashlib
import re
import struct

P256_SPKI = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
ESP_MAGIC = 0xE9


class BootIdError(ValueError):
    pass


def esp_image_length(data):
    """Length of the ESP32 app-format image at the start of `data`, or None if it is not one.
    Header: 8 bytes, extended header 16 (byte 23 = hash appended); then each segment: 8-byte
    header + data; then padding to 16 with the checksum byte last; then 32 bytes of SHA-256
    if appended."""
    if len(data) < 24 or data[0] != ESP_MAGIC:
        return None
    nseg = data[1]
    hash_appended = data[23] == 1
    off = 24
    for _ in range(nseg):
        if off + 8 > len(data):
            return None
        _addr, size = struct.unpack_from("<II", data, off)
        off += 8 + size
        if off > len(data):
            return None
    off += 1                          # the checksum byte
    off = (off + 15) & ~15            # padded so the checksum ends a 16-byte block
    if hash_appended:
        off += 32
    return off if off <= len(data) else None


def identify(data, family="esp32"):
    """The identity of the bootloader at the start of `data` (a file, or a flash read)."""
    if family == "esp32":
        n = esp_image_length(data)
        if n is None:
            blank = not data.strip(b"\xff")
            return {"kind": "blank" if blank else "unknown", "key": None, "built": None,
                    "sha256": None, "length": None}
        data = data[:n]
    i = data.find(P256_SPKI)
    key = None
    if i >= 0 and i + len(P256_SPKI) + 65 <= len(data):
        key = hashlib.sha256(data[i:i + len(P256_SPKI) + 65]).hexdigest()[:16]
    m = re.search(rb"compile time (\w{3} +\d+ \d{4} \d\d:\d\d:\d\d)", data)
    is_mcuboot = b"MCUboot" in data or b"mcuboot" in data
    return {"kind": "mcuboot" if is_mcuboot else "other", "key": key,
            "built": m.group(1).decode() if m else None,
            "sha256": hashlib.sha256(data).hexdigest(), "length": len(data)}


POLICIES = ("key", "exact")


def decide(want, have, policy="key"):
    """(write?, why). `key`: write when the device has no MCUboot or one with another key
    (it would not boot the bench's images). `exact`: also when the bytes differ (another build)."""
    if policy not in POLICIES:
        raise BootIdError(f"bootloader policy must be one of {', '.join(POLICIES)}")
    if want.get("kind") != "mcuboot" or not want.get("key"):
        raise BootIdError("the staged bootloader is not an MCUboot with a signing key")
    if have.get("sha256") == want["sha256"]:
        return False, "the same bootloader"
    if have.get("kind") != "mcuboot":
        return True, f"the device has no MCUboot ({have.get('kind')})"
    if have.get("key") != want["key"]:
        return True, (f"its MCUboot verifies another key ({have.get('key')}, want "
                      f"{want['key']}): it would refuse the bench's images")
    if policy == "exact":
        return True, f"another build of the same MCUboot ({have.get('built')} -> {want['built']})"
    return False, f"same key, another build ({have.get('built')}); policy `key` keeps it"
