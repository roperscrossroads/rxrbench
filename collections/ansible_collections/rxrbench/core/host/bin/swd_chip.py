#!/usr/bin/env python3
"""A microcontroller behind an SWD probe: identify, read, write, erase. One openocd session each.

    swd_chip.py id [--look]                {dpidr, idcode, flash_size, uid, voltage}; with
                                           --look the core is neither halted nor reset (the
                                           firmware runs on): what a survey reads
    swd_chip.py sha   <length>             {sha256} of <length> bytes of flash from its start
    swd_chip.py read  <out.bin> [length]   the flash (all of it by default), to a file
    swd_chip.py write <in.bin>             erase what the image covers, program, verify, reset
    swd_chip.py erase [length]             erase the whole flash, or with --at, <length> bytes
                                           from there (whole sectors only: openocd refuses a
                                           range that does not start and end on a sector)

Common options: `--chip <part>` (STM32F103C8, nRF52840_xxAA, ...: picks the row of CHIPS),
`--probe <kind>` (stlink, cmsis-dap), `--location <bus-ports>` (which probe: its USB location,
`devreg resolve --busid`; without it openocd takes the first probe of that kind it finds, the
wrong one when a host has two), `--uid <hex>`, `--srst` (the probe's reset line is wired
to the chip's NRST: connect with the chip held in reset), `--at <address>` (sha, read and
write start there instead of at the start of flash: an image linked behind a bootloader; an
address outside the chip's flash, or an image running past its end, is refused before
anything is written).

Without `--srst` the chip must be reachable while it runs. Many are not: an STM32F1 whose
firmware sleeps (WFI) stops its debug clock unless that firmware set DBGMCU's sleep bits, and
then only a connect under reset gets in. The error says so when the debug port answers and
the core does not.

`--uid` is the guard. A probe says nothing about what is wired to it, so the chip's own
unique id is read first, inside the same session as the work, and the session ends there
when it is not the one given. `write` and `erase` refuse to run without it.

Each prints one JSON object on stdout; a failure is {"error": ...} and exit status 1, with
"unanswered": true when the chip did not answer at all (nothing was read or done). The
chip is left running (reset) after everything but `erase`, which leaves nothing to run.

Standard library only; needs `openocd` on PATH (rxrbench's default install).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

# What differs between chip families, as data. `uid`: (address, 32-bit words). `size`: how the
# flash size in bytes is read (Tcl, leaving it in $size). `erase`: the mass-erase command.
CHIPS = {
    "stm32f1": {
        "target": "target/stm32f1x.cfg", "base": 0x08000000, "uid": (0x1FFFF7E8, 3),
        "idcode": 0xE0042000,
        "size": "set size [expr {([read_memory 0x1FFFF7E0 16 1] & 0xffff) * 1024}]",
        "erase": "stm32f1x mass_erase 0",
    },
    # STM32F7 (F74x/F75x/F76x/F77x): openocd drives its flash with the stm32f2x driver. Sectors
    # are not even (on a 2 MB F767 in single-bank mode: 4 x 32 KB, 128 KB, 7 x 256 KB), so a
    # ranged erase must start and end on one of those boundaries.
    "stm32f7": {
        "target": "target/stm32f7x.cfg", "base": 0x08000000, "uid": (0x1FF0F420, 3),
        "idcode": 0xE0042000,
        "size": "set size [expr {([read_memory 0x1FF0F442 16 1] & 0xffff) * 1024}]",
        "erase": "stm32f2x mass_erase 0",
    },
    # Not proven on hardware yet. An nRF52 with APPROTECT set does not answer
    # here at all, and an ST-Link cannot lift it (that needs the CTRL-AP, i.e. CMSIS-DAP).
    "nrf52": {
        "target": "target/nrf52.cfg", "base": 0x00000000, "uid": (0x10000060, 2),
        "idcode": 0x10000100,
        "size": "set size [expr {[read_memory 0x10000010 32 1] * [read_memory 0x10000014 32 1]}]",
        "erase": "nrf5 mass_erase",
    },
}
PROBES = {"stlink": "interface/stlink.cfg", "cmsis-dap": "interface/cmsis-dap.cfg"}
# A USB location as sysfs names it and openocd's `adapter usb location` takes it: bus-port.port...
USB_LOCATION = re.compile(r"^\d+-\d+(\.\d+)*$")


class SwdError(Exception):
    """`unanswered`: nothing was read or done because the chip did not answer (not wired, not
    powered, asleep). The caller skips such a board instead of failing it."""
    def __init__(self, msg, unanswered=False):
        super().__init__(msg)
        self.unanswered = unanswered


def chip_row(part):
    p = part.lower()
    for prefix, row in CHIPS.items():
        if p.startswith(prefix):
            return row
    raise SwdError(f"no SWD recipe for chip {part!r} (known: {', '.join(CHIPS)})")


def tcl_prelude(row, uid, halt=True):
    """Tcl that connects, halts (unless not `halt`: the id registers are read through the
    debug port while the core runs), reads what the chip is and stops if it is not `uid`."""
    addr, words = row["uid"]
    lines = [
        "init", *(["reset halt"] if halt else []),
        f"set uid [read_memory {addr:#x} 32 {words}]",
        'set uidhex ""',
        'foreach w $uid { append uidhex [format %08x $w] }',
        f"set idcode [read_memory {row['idcode']:#x} 32 1]",
        row["size"],
        'echo "~uid $uidhex"', 'echo "~idcode [format %08x $idcode]"', 'echo "~size $size"',
    ]
    if not halt:
        # Read while the core runs, the words may be stale (a core asleep with its debug clock
        # off: seen on an STM32F103 as the unique id shifted by a word and the flash size a
        # piece of it). They are read again and must say the same, or nothing is reported.
        lines += [
            f"set again [read_memory {addr:#x} 32 {words}]",
            f"set idcode2 [read_memory {row['idcode']:#x} 32 1]",
            "set first $size",
            row["size"],
            'if {$again ne $uid || $idcode2 ne $idcode || $size ne $first} '
            '{ echo "~unsteady"; shutdown error }',
        ]
    if uid:
        lines.append(f'if {{$uidhex ne "{uid.lower()}"}} {{ echo "~refused uid"; shutdown error }}')
    return lines


def run(probe, row, body, uid=None, timeout=120, srst=False, halt=True):
    """One openocd session: the prelude, then `body` (Tcl lines). Returns ({marker: value},
    whole output). Raises SwdError on a refusal, a failure or a timeout. `probe`: its kind, or
    (kind, USB location) to name which probe of that kind."""
    probe, where = probe if isinstance(probe, tuple) else (probe, None)
    if where is not None and not USB_LOCATION.match(where):
        raise SwdError(f"{where!r} is not a USB location (bus-port.port..., as devreg "
                       "resolve --busid prints it)")
    if probe not in PROBES:
        raise SwdError(f"unknown probe kind {probe!r} (known: {', '.join(PROBES)})")
    script = "\n".join(tcl_prelude(row, uid, halt) + list(body) + ["shutdown"]) + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as f:
        f.write(script)
    argv = ["openocd", "-f", PROBES[probe]]
    if where:
        argv += ["-c", f"adapter usb location {where}"]
    argv += ["-c", "transport select swd", "-f", row["target"]]
    if srst:
        argv += ["-c", "reset_config srst_only srst_nogate connect_assert_srst"]
    argv += ["-f", f.name]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        out, rc = r.stdout + r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        raise SwdError(f"openocd did not finish in {timeout} s") from None
    except OSError as e:
        raise SwdError(f"cannot run openocd: {e}") from e
    finally:
        os.unlink(f.name)
    marks = dict(m.groups() for m in re.finditer(r"^~(\w+) ?(.*)$", out, re.M))
    for pattern, key in ((r"SWD DPIDR (0x[0-9a-fA-F]+)", "dpidr"),
                         (r"Target voltage: ([0-9.]+)", "voltage")):
        m = re.search(pattern, out)
        if m:
            marks[key] = m.group(1)
    if "unsteady" in marks:
        raise SwdError("the chip's identity read differently twice while it ran (a core asleep "
                       "with its debug clock off answers with stale words): read it halted "
                       "(swd_chip.py id without --look), or under reset where NRST is wired")
    if "refused" in marks:
        raise SwdError(f"the chip behind the probe has unique id {marks.get('uid')}, "
                       f"not {uid}: nothing was done")
    if "toolong" in marks:
        raise SwdError(f"it runs past the end of the chip's flash ({marks.get('size')} bytes): "
                       "nothing was written")
    if "uid" not in marks and re.search(
            r"Examination failed|AP write error|Fail(ed)? reading|DP fault|unable to connect",
            out, re.I):
        raise SwdError(
            ("the debug port answers (DPIDR " + marks["dpidr"] + ") but the core does not"
             if "dpidr" in marks else "the probe is there but the chip does not answer it")
            + ": not wired, not powered, or asleep with its debug clock off"
            + ("" if srst else ". For a sleeping chip: wire the probe's reset line to NRST and "
               "declare it (--srst), or start the chip in its ROM bootloader (BOOT0)"),
            unanswered=True)
    if rc != 0 or "uid" not in marks:
        tail = " | ".join(x for x in out.strip().splitlines()[-4:])
        raise SwdError(f"openocd failed (rc {rc}): {tail}")
    return marks, out


def identity(marks):
    out = {"uid": marks["uid"], "idcode": "0x" + marks["idcode"], "flash_size": int(marks["size"])}
    if "dpidr" in marks:
        out["dpidr"] = marks["dpidr"].lower()
    if "voltage" in marks:
        out["voltage"] = round(float(marks["voltage"]), 2)
    return out


def start(row, at):
    """Where in flash a read or write starts: `at` (an address) or the start of flash."""
    if at is None:
        return row["base"]
    if at < row["base"]:
        raise SwdError(f"{at:#x} is below the start of this chip's flash ({row['base']:#x})")
    return at


def fits(row, at, length):
    """Tcl that stops the session when `length` bytes from `at` do not fit in the flash."""
    return (f"if {{{at - row['base'] + length} > $size}} {{ echo \"~toolong\"; shutdown error }}")


def dump(probe, row, length, uid, path, srst=False, at=None):
    at = start(row, at)
    marks, _ = run(probe, row, [
        fits(row, at, length) if length else "",
        f"dump_image {path} {at:#x} " + (str(length) if length else f"[expr {{$size - {at - row['base']}}}]"),
        "reset run"], uid, srst=srst)
    return marks


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("verb", choices=["id", "sha", "read", "write", "erase"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--chip", required=True)
    ap.add_argument("--probe", default="stlink")
    ap.add_argument("--uid")
    ap.add_argument("--srst", action="store_true")
    ap.add_argument("--location")
    ap.add_argument("--look", action="store_true")
    ap.add_argument("--at", type=lambda v: int(v, 0), default=None)
    a = ap.parse_args(argv)
    if a.location is not None:
        a.probe = (a.probe, a.location)
    try:
        row = chip_row(a.chip)
        if a.verb in ("write", "erase") and not a.uid:
            raise SwdError(f"{a.verb} needs --uid: the chip's unique id is the only proof of "
                           "what is wired to the probe")
        if a.look and (a.verb != "id" or a.srst):
            raise SwdError("--look is for id alone, and not with --srst (connecting under reset "
                           "resets the chip)")
        if a.verb == "id":
            marks, _ = run(a.probe, row, [] if a.look else ["reset run"], a.uid, srst=a.srst,
                           halt=not a.look)
            out = identity(marks)
        elif a.verb in ("sha", "read"):
            want = int(a.args[0 if a.verb == "sha" else 1], 0) if len(a.args) > (a.verb == "read") else 0
            if a.verb == "sha" and not want:
                raise SwdError("sha needs a length")
            if a.verb == "read":
                path = os.path.abspath(a.args[0])
            else:
                fd, path = tempfile.mkstemp(suffix=".bin")
                os.close(fd)
            try:
                marks = dump(a.probe, row, want, a.uid, path, a.srst, a.at)
                with open(path, "rb") as f:
                    data = f.read()
            finally:
                if a.verb == "sha" and os.path.exists(path):
                    os.unlink(path)
            out = {**identity(marks), "length": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}
        elif a.verb == "write":
            path = os.path.abspath(a.args[0])
            with open(path, "rb") as f:
                data = f.read()
            at = start(row, a.at)
            marks, text = run(a.probe, row, [
                fits(row, at, len(data)),
                f"flash write_image erase {path} {at:#x} bin",
                f"verify_image {path} {at:#x} bin",
                'echo "~verified"', "reset run"], a.uid, srst=a.srst)
            if "verified" not in marks:
                raise SwdError("the write was not verified: " + text.strip()[-300:])
            out = {**identity(marks), "length": len(data),
                   "sha256": hashlib.sha256(data).hexdigest(), "written": True}
        elif a.at is not None or a.args:
            if a.at is None or len(a.args) != 1:
                raise SwdError("a ranged erase needs both --at and a length: nothing is erased "
                               "from a guessed place")
            at, length = start(row, a.at), int(a.args[0], 0)
            if length <= 0:
                raise SwdError(f"length {length} is not positive")
            marks, _ = run(a.probe, row, [fits(row, at, length),
                                          f"flash erase_address {at:#x} {length}",
                                          'echo "~erased"', "reset run"], a.uid, srst=a.srst)
            if "erased" not in marks:
                raise SwdError("the erase did not finish")
            out = {**identity(marks), "erased": True, "at": at, "length": length}
        else:
            marks, _ = run(a.probe, row, [row["erase"], 'echo "~erased"'], a.uid, srst=a.srst)
            if "erased" not in marks:
                raise SwdError("the erase did not finish")
            out = {**identity(marks), "erased": True}
    except (SwdError, OSError, ValueError, IndexError) as e:
        print(json.dumps({"error": str(e),
                          **({"unanswered": True} if getattr(e, "unanswered", False) else {})}))
        return 1
    # where the chip's flash starts: a caller that knows addresses only as offsets of it
    out["flash_base"] = row["base"]
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
