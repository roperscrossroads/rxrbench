#!/usr/bin/env python3
"""An nRF52840's factory and user information, read over SWD without changing anything.

    nrf_ids.py [--probe VID:PID[:SERIAL]] [--connect-under-reset] [--raw-out FILE]

FICR (0x10000000) is written by Nordic and cannot be erased: the device id and address, the
part, package, RAM and flash sizes. UICR (0x10001000) is written by whoever flashed the
board and is cleared only by a whole-chip erase: the bootloader address the MBR jumps to,
the regulator output voltage, the NFC pin setting, debug protection, customer words.

Prints one JSON object. Words are read with `probe-rs read`; `--allow-erase-all` is never
passed, so a chip with debug protection set is refused, not unlocked (unlocking erases it).
The encryption and identity roots (FICR ER/IR) are secrets: they are left out of the output
and are only written, to a file of mode 600, when `--raw-out` asks for the raw words.

Standard library only; needs `probe-rs` on PATH (rxrbench's default install). The decoding
is tested against a synthetic memory image, not yet against a chip.
"""
import argparse
import json
import os
import re
import subprocess
import sys

FICR, FICR_WORDS = 0x10000000, 0x80     # 0x200 bytes: everything decoded below
UICR, UICR_WORDS = 0x10001000, 0xC4     # 0x310 bytes: up to and including REGOUT0 at 0x304
REGOUT0 = {0: "1.8 V", 1: "2.1 V", 2: "2.4 V", 3: "2.7 V", 4: "3.0 V", 5: "3.3 V",
           7: "default (1.8 V)"}
ERASED = 0xFFFFFFFF


class IdsError(Exception):
    pass


def w(words, offset):
    return words[offset // 4]


def decode_ficr(words):
    """The FICR words (from 0x10000000) -> what is safe and useful to keep."""
    addr = w(words, 0xA4) | ((w(words, 0xA8) & 0xFFFF) << 32)
    variant = w(words, 0x104).to_bytes(4, "big")
    return {
        "device_id": f"{w(words, 0x64):08x}{w(words, 0x60):08x}",
        "device_addr": ":".join(f"{(addr >> s) & 0xFF:02X}" for s in range(40, -8, -8)),
        "device_addr_type": "random" if w(words, 0xA0) & 1 else "public",
        "part": f"nRF{w(words, 0x100):X}",
        "variant": variant.decode("ascii") if variant.isalnum() else f"0x{variant.hex()}",
        "package": f"0x{w(words, 0x108):08x}",
        "ram_kb": w(words, 0x10C),
        "flash_kb": w(words, 0x110),
        "code_page_size": w(words, 0x10),
        "code_pages": w(words, 0x14),
        "er_ir": "withheld",
    }


def decode_uicr(words):
    """The UICR words (from 0x10001000). 0xFFFFFFFF is an erased word, i.e. not set."""
    def val(offset):
        v = w(words, offset)
        return None if v == ERASED else v

    approtect = w(words, 0x208) & 0xFF
    regout = w(words, 0x304)
    out = {
        "bootloader_addr": val(0x14),
        "bootloader_settings_addr": val(0x18),
        "regout0": "not set" if regout == ERASED else REGOUT0.get(regout & 7, f"{regout & 7}?"),
        # NFCPINS.PROTECT, bit 0: 1 = P0.09/P0.10 are the NFC antenna pins (the erased default),
        # 0 = they are ordinary GPIOs (what a board that uses them sets).
        "nfc_pins": "nfc" if w(words, 0x20C) & 1 else "gpio",
        "approtect": {0x00: "enabled", 0xFF: "disabled"}.get(approtect, f"0x{approtect:02x}?"),
        "pselreset": [val(0x200), val(0x204)],
        "nrffw": [w(words, 0x14 + 4 * i) for i in range(13)],
        "nrfhw": [w(words, 0x50 + 4 * i) for i in range(12)],
        "customer": {i: f"0x{w(words, 0x80 + 4 * i):08x}" for i in range(32)
                     if w(words, 0x80 + 4 * i) != ERASED},
    }
    return out


def read_words(address, count, probe, under_reset):
    argv = ["probe-rs", "read", "--chip", "nRF52840_xxAA", "--protocol", "swd",
            "--non-interactive"]
    if probe:
        argv += ["--probe", probe]
    if under_reset:
        argv.append("--connect-under-reset")
    argv += ["b32", f"{address:#x}", str(count)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise IdsError(f"cannot run probe-rs: {e}") from e
    if r.returncode != 0:
        raise IdsError("probe-rs failed (not wired, not powered, or debug protection set): "
                       + (r.stderr or r.stdout).strip()[-300:])
    words = [int(x, 16) for x in re.findall(r"\b[0-9a-fA-F]{8}\b", r.stdout)]
    if len(words) != count:
        raise IdsError(f"expected {count} words at {address:#x}, got {len(words)}")
    return words


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probe")
    ap.add_argument("--connect-under-reset", action="store_true")
    ap.add_argument("--raw-out", help="write the raw FICR and UICR words here (mode 600)")
    a = ap.parse_args(argv)
    try:
        ficr = read_words(FICR, FICR_WORDS, a.probe, a.connect_under_reset)
        uicr = read_words(UICR, UICR_WORDS, a.probe, a.connect_under_reset)
        out = {"ficr": decode_ficr(ficr), "uicr": decode_uicr(uicr)}
        if a.raw_out:
            fd = os.open(a.raw_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"ficr_0x10000000": [f"{x:08x}" for x in ficr],
                           "uicr_0x10001000": [f"{x:08x}" for x in uicr]}, f, indent=1)
            out["raw_out"] = a.raw_out
    except (IdsError, IndexError) as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
