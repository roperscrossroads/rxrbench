"""Images built elsewhere (docs/ARCHITECTURE.md#core-portability): an Intel HEX or
ELF file becomes one raw binary and the address it goes at, once, at staging."""
import hashlib
import json
import os
import struct
import subprocess
import sys

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import imagefile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def record(typ, addr, data):
    body = bytes([len(data), (addr >> 8) & 0xFF, addr & 0xFF, typ]) + data
    return ":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper()


def ihex(pieces, start=None):
    """An Intel HEX file of [(address, bytes)], with extended linear address records."""
    lines, upper = [], None
    for addr, data in pieces:
        for i in range(0, len(data), 16):
            a = addr + i
            if a >> 16 != upper:
                upper = a >> 16
                lines.append(record(4, 0, upper.to_bytes(2, "big")))
            lines.append(record(0, a & 0xFFFF, data[i:i + 16]))
    if start is not None:
        lines.append(record(5, 0, start.to_bytes(4, "big")))
    lines.append(record(1, 0, b""))
    return "\n".join(lines) + "\n"


def elf32(segments):
    """A little-endian ELF32 file with one PT_LOAD per (paddr, bytes, vaddr)."""
    phoff, phsize = 52, 32
    data_off = phoff + phsize * len(segments)
    header = b"\x7fELF" + bytes([1, 1, 1]) + b"\0" * 9 + struct.pack(
        "<HHIIIIIHHHHHH", 2, 40, 1, 0, phoff, 0, 0, 52, phsize, len(segments), 0, 0, 0)
    phdrs, blobs, off = b"", b"", data_off
    for paddr, data, vaddr in segments:
        phdrs += struct.pack("<IIIIIIII", 1, off, vaddr, paddr, len(data), len(data), 5, 4)
        blobs += data
        off += len(data)
    return header + phdrs + blobs


def test_a_hex_behind_a_bootloader_becomes_its_bytes_and_its_address(tmp_path):
    p = tmp_path / "app.hex"
    p.write_text(ihex([(0x08004000, b"A" * 40), (0x08004030, b"B" * 8)], start=0x08004101))
    data, base, spans = imagefile.flatten(str(p))
    assert base == 0x08004000 and spans == [(0x08004000, 0x08004038)]
    assert data == b"A" * 40 + b"\xff" * 8 + b"B" * 8, "a hole reads as erased flash"


def test_an_elf_is_written_at_its_load_addresses_not_where_it_runs(tmp_path):
    p = tmp_path / "app.elf"
    # .text in flash; .data's bytes stored in flash after it, copied to RAM at boot
    p.write_bytes(elf32([(0x00027000, b"T" * 32, 0x00027000), (0x00027020, b"D" * 8, 0x20000000)]))
    data, base, _ = imagefile.flatten(str(p))
    assert imagefile.kind_of(str(p)) == "elf"
    assert base == 0x27000 and data == b"T" * 32 + b"D" * 8


@pytest.mark.parametrize("pieces,why", [
    ([(0x0, b"A" * 16), (0x10001000, b"U" * 4)], "configuration area"),
    ([(0x0, b"A" * 16), (0x8, b"B" * 16)], "overlap"),
    ([], "no bytes to write"),
])
def test_what_the_bench_would_write_wrong_is_refused(tmp_path, pieces, why):
    p = tmp_path / "x.hex"
    p.write_text(ihex(pieces))
    with pytest.raises(imagefile.ImageFileError, match=why):
        imagefile.flatten(str(p))


F767 = (0x08000000, 0x08200000)    # a NUCLEO-F767ZI's 2 MB


def test_a_bootloader_and_an_app_in_its_slot_are_one_image_when_the_flash_is_known(tmp_path):
    """MCUboot at the start of flash and the signed app 256 KB on: both are flash, so one image
    with the erased flash between them, however far apart (the gap rule alone refused it)."""
    p = tmp_path / "merged.hex"
    p.write_text(ihex([(0x08000000, b"M" * 16), (0x08040000, b"A" * 16)]))
    with pytest.raises(imagefile.ImageFileError, match="areas apart"):
        imagefile.flatten(str(p))
    data, base, spans = imagefile.flatten(str(p), flash=F767)
    assert base == 0x08000000 and spans == [(0x08000000, 0x08040010)]
    assert data[:16] == b"M" * 16 and data[0x40000:] == b"A" * 16 and set(data[16:0x40000]) == {0xFF}


def test_a_piece_outside_the_known_flash_is_refused_and_named(tmp_path):
    p = tmp_path / "x.hex"
    p.write_text(ihex([(0x08000000, b"A" * 16), (0x1FFF0000, b"O" * 8)]))   # option bytes
    with pytest.raises(imagefile.ImageFileError, match="outside the chip's flash .*0x1fff0000-0x1fff0008"):
        imagefile.flatten(str(p), flash=F767)


@pytest.mark.parametrize("board,area", [
    ({"flash_base": "0x08000000", "flash": "2MB"}, F767),
    ({"flash_base": 0x10000000, "flash": "16MB"}, (0x10000000, 0x11000000)),
    ({"flash_base": "0x0", "flash": "0x1B"}, (0, 0x1B)),
    ({"flash": "16MB"}, None),           # how much but not where: the gap rule stays
    ({}, None),
])
def test_where_a_board_types_flash_is(board, area):
    assert imagefile.flash_area(board) == area


def test_a_damaged_record_is_refused(tmp_path):
    p = tmp_path / "x.hex"
    good = record(0, 0, b"ABCD")
    p.write_text(good[:-2] + "00\n" + record(1, 0, b"") + "\n")
    with pytest.raises(imagefile.ImageFileError, match="checksum"):
        imagefile.flatten(str(p))


def stage(tmp_path, image, *extra):
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "python"), os.path.join(REPO, "tools", "stage_raw.py"),
                        str(image), "--name", "app", "--board-tag", "bluepill-f103",
                        "--store", str(tmp_path / "store"), *extra],
                       capture_output=True, text=True, check=False)
    return r.returncode, r.stdout + r.stderr


def test_staging_a_hex_keeps_the_original_and_records_where_it_goes(tmp_path):
    src = tmp_path / "app.hex"
    src.write_text(ihex([(0x08004000, b"Z" * 64)]))
    rc, out = stage(tmp_path, src)
    assert rc == 0, out
    d = tmp_path / "store" / "bare" / "app"
    m = json.loads((d / "manifest.json").read_text())
    assert m["format"] == "raw-bin" and m["file"] == "image.bin" and m["base"] == "0x8004000"
    assert m["hash"] == hashlib.sha256(b"Z" * 64).hexdigest() == hashlib.sha256((d / "image.bin").read_bytes()).hexdigest()
    assert m["converted_from"] == {"file": "app.hex", "format": "ihex",
                                   "sha256": hashlib.sha256(src.read_bytes()).hexdigest()}
    assert (d / "app.hex").read_bytes() == src.read_bytes()


def test_staging_reads_where_the_flash_is_from_the_board_type(tmp_path, monkeypatch):
    site = tmp_path / "site"
    (site / "catalog").mkdir(parents=True)
    (site / "catalog" / "boards.yml").write_text(
        "boards:\n  nucleo-f767zi: {mcu: stm32f767, flash_base: '0x08000000', flash: 2MB}\n")
    monkeypatch.setenv("RXRBENCH_SITE", str(site))
    src = tmp_path / "merged.hex"
    src.write_text(ihex([(0x08000000, b"M" * 16), (0x08040000, b"A" * 16)]))
    rc, out = stage(tmp_path, src, "--board-tag", "nucleo-f767zi")
    assert rc == 0, out
    m = json.loads((tmp_path / "store" / "bare" / "app" / "manifest.json").read_text())
    assert m["base"] == "0x8000000" and m["board_tag"] == "nucleo-f767zi"
    rc, out = stage(tmp_path, src)        # a type that does not say where its flash is
    assert rc != 0 and "areas apart" in out


def test_a_bin_is_staged_as_before_and_a_refused_file_stages_nothing(tmp_path):
    raw = tmp_path / "app.bin"
    raw.write_bytes(b":not a hex file")
    rc, out = stage(tmp_path, raw)
    m = json.loads((tmp_path / "store" / "bare" / "app" / "manifest.json").read_text())
    assert rc == 0 and "base" not in m and m["file"] == "app.bin", out
    bad = tmp_path / "two.hex"
    bad.write_text(ihex([(0x0, b"A" * 16), (0x10001000, b"U" * 4)]))
    rc, out = stage(tmp_path, bad, "--family", "other")
    assert rc != 0 and "configuration area" in out
    assert not (tmp_path / "store" / "other").exists()
