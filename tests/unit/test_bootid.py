import hashlib
import struct

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import bootid

KEY_A = bootid.P256_SPKI + bytes(range(65))
KEY_B = bootid.P256_SPKI + bytes(range(1, 66))


def esp_image(body, hash_appended=True, built=b"Oct  2 2026 13:28:49"):
    """A minimal ESP32 app-format image: header, extended header, one segment."""
    seg = b"MCUboot 2nd stage bootloader compile time " + built + b"\0" + body
    seg += b"\0" * (-len(seg) % 4)
    hdr = bytes([0xE9, 1, 2, 0x4F]) + struct.pack("<I", 0x403C8000)
    ext = bytes(15) + bytes([1 if hash_appended else 0])
    img = hdr + ext + struct.pack("<II", 0x3FCE0000, len(seg)) + seg
    img += b"\0" * (15 - len(img) % 16) + b"\xEF"          # padding, checksum ends the block
    if hash_appended:
        img += hashlib.sha256(img).digest()
    return img


def test_the_image_length_comes_from_its_header_so_a_partition_read_hashes_like_the_file():
    img = esp_image(KEY_A)
    part = img + b"\xff" * (0x10000 - len(img))
    assert bootid.esp_image_length(part) == len(img)
    assert bootid.identify(part) == bootid.identify(img)
    assert bootid.identify(img)["built"] == "Oct  2 2026 13:28:49"


def test_erased_flash_is_blank_and_has_no_key():
    i = bootid.identify(b"\xff" * 0x10000)
    assert i["kind"] == "blank" and i["key"] is None


def test_the_key_policy_keeps_another_build_with_the_same_key():
    want = bootid.identify(esp_image(KEY_A))
    old = bootid.identify(esp_image(KEY_A + b"x", built=b"Aug 31 2026 22:10:26"))
    assert old["key"] == want["key"] and old["sha256"] != want["sha256"]
    assert bootid.decide(want, old, "key")[0] is False
    write, why = bootid.decide(want, old, "exact")
    assert write and "Aug 31" in why


def test_another_key_or_no_mcuboot_is_always_written():
    want = bootid.identify(esp_image(KEY_A))
    assert bootid.decide(want, bootid.identify(esp_image(KEY_B)))[0]
    assert bootid.decide(want, bootid.identify(b"\xff" * 4096))[0]
    assert bootid.decide(want, want) == (False, "the same bootloader")


def test_a_staged_file_that_is_not_mcuboot_with_a_key_is_refused():
    with pytest.raises(bootid.BootIdError, match="not an MCUboot"):
        bootid.decide(bootid.identify(b"\xff" * 64), bootid.identify(b"\xff" * 64))
