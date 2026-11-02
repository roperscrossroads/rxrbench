"""A whole ESP32 flash chip: read it, check it, write it back. Shared by chip_backup and
chip_restore.

The work is done on the bench host by `chip_blocks.py` (host/bin) in one ROM-loader session:
the loader's stub hashes every 64 KB block itself, only blocks that are not erased are read
(most of a chip is erased, and the USB-Serial-JTAG reads ~100 KB/s), and a restore writes only
the blocks that differ from the image. Each block read or written is checked against the
chip's own hash. The board does not boot during the session, so it cannot change its config
in the middle of it (a running node writes its `storage` page at every boot).

Then a separate, fresh esptool session boots the board (reset()): esptool 5.4's hard reset from
inside a session leaves an ESP32-S3 on the USB-Serial-JTAG in its loader ("Hard resetting",
then silence).

The chip is identified by its factory MAC (eFuse), read by the loader itself, not by anything
the firmware says: a chip image holds the node's private key, and writing one board's image to
another would make two boards the same node.
"""
import contextlib
import functools
import gzip
import hashlib
import json
import os
import shutil

from . import common



def sha256_file(path, opener=open):
    h = hashlib.sha256()
    with opener(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


DEFAULT_TOOLS = {"esptool": "~/.venv-esp/bin/esptool", "python": "~/.venv-esp/bin/python",
                 "chip_blocks": "~/rxrbench/bin/chip_blocks.py"}


def tools_of(given, chip=None):
    """The tools the chip is read and written with, and `chip`: what esptool calls it."""
    out = dict(DEFAULT_TOOLS)
    out.update({k: v for k, v in (given or {}).items() if v})
    out = {k: os.path.expanduser(v) for k, v in out.items()}
    out["chip"] = chip
    return out


#: How chip_blocks.py's usage text lists a command (it prints the text when it is called wrongly).
USAGE = "chip_blocks.py <port> "


def stale_copy(tools, cmd, text, out):
    """Why a bench host's chip_blocks.py did not run `cmd`, when the reason is its age: the
    copy answered that it has no such command (`unknown_command`, from a copy that says so),
    or printed a usage text that does not list it (a copy from before it said so). None when
    the copy has the command."""
    if out.get("unknown_command") == cmd or (USAGE in text and USAGE + cmd not in text):
        return (f"the bench host's chip_blocks.py ({tools['chip_blocks']}) has no `{cmd}` "
                "command: it is older than this checkout. Bring the host's tools up to date "
                "with `just hosts-setup <layout>`; nothing was written")
    return None


def blocks(tools, port, *args, timeout=900):
    """Run chip_blocks.py; its last output line is one JSON object."""
    rc, text = common.tool([tools["python"], tools["chip_blocks"], port, *args], timeout=timeout)
    last = text.strip().splitlines()[-1] if text.strip() else ""
    try:
        out = json.loads(last)
    except ValueError:
        out = {"ok": False, "error": text.strip()[-300:]}
    if rc != 0 or out.get("ok") is False:
        stale = stale_copy(tools, args[0], text, out)
        if stale:
            raise common.FlashError(stale)
        if USAGE in text:
            out["error"] = (f"it printed its usage: called with arguments `{args[0]}` does not "
                            "take (" + " ".join(str(a) for a in args) + "); if this checkout is "
                            "newer than the bench host's copy, `just hosts-setup <layout>`")
        raise common.FlashError(f"chip_blocks {args[0]} failed (rc {rc}): {out.get('error')}")
    out["flash_size"] = f"{out['size'] >> 20}MB"
    return out


def blocks_if_installed(tools):
    """`blocks` with these tools bound, when chip_blocks.py and its python are on this host:
    then the write rule reads the chip in one loader session (common.esp_write_refusals). None
    otherwise, and the rule reads range by range with esptool."""
    if tools and all(os.path.exists(tools.get(k) or "") for k in ("python", "chip_blocks")):
        return functools.partial(blocks, tools)
    return None


def info(tools, port):
    """The chip as its loader reports it: {mac, size, flash_size, chip}. Boots it after."""
    with booted_after(tools["esptool"], port, tools["chip"]):
        return blocks(tools, port, "info", timeout=120)


def read(tools, port, path):
    """The whole chip into `path` (erased blocks as 0xFF), then boot the board. Returns the
    chip's info and what was read (blocks, read_blocks, seconds)."""
    with booted_after(tools["esptool"], port, tools["chip"]):
        out = blocks(tools, port, "read", path)
    if not os.path.exists(path) or os.path.getsize(path) != out["size"]:
        raise common.FlashError("the chip read left no whole image")
    return out


def write(tools, port, path):
    """`path` onto the whole chip, writing only the blocks that differ, then boot the board.
    Returns the chip's info and what was written (blocks, written_blocks, seconds)."""
    with booted_after(tools["esptool"], port, tools["chip"]):
        return blocks(tools, port, "write", path)


def differ(tools, port, path):
    """How many blocks of the chip differ from `path` (what `write` would write), then boot
    the board. Nothing is written."""
    with booted_after(tools["esptool"], port, tools["chip"]):
        return blocks(tools, port, "diff", path)


def erase(tools, port, mac):
    """The whole chip erased, in the loader session that reads its MAC (refused unless it is
    `mac`), every block read erased after; then the board is reset (it has nothing to boot: it
    stays in its ROM). Returns the chip's info and the erase's (blocks, seconds)."""
    with booted_after(tools["esptool"], port, tools["chip"]):
        return blocks(tools, port, "erase", "--mac", mac, timeout=900)


def _esptool_reset(esptool, port, chip):
    return common.tool([esptool, "--chip", chip, "--port", port, "--before", "default-reset",
                        "--after", "hard-reset", "chip-id"], timeout=60)


def reset(esptool, port, chip):
    """Boot the board out of the loader: a fresh session (esptool's own reset sequence into
    the loader, then a hard reset), which is what reliably boots the app."""
    rc, text = _esptool_reset(esptool, port, chip)
    if rc != 0:
        raise common.FlashError(f"resetting the board failed (rc {rc}): {text.strip()[-300:]}")


@contextlib.contextmanager
def booted_after(esptool, port, chip):
    """Boot the board when the block ends, also when it fails (a board left in its loader
    looks dead); a failed reset does not hide the block's own error."""
    try:
        yield
    except BaseException:
        try:
            reset(esptool, port, chip)
        except common.FlashError:
            pass
        raise
    reset(esptool, port, chip)


def compress(src, dst):
    with open(src, "rb") as fi, gzip.open(dst, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)


def expand(src, dst):
    with gzip.open(src, "rb") as fi, open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)


def check_backup(meta, chip, board, digest):
    """Refusals for writing a chip backup whose record is `meta` (and whose image hashes to
    `digest`) to `board`, whose chip reads as `chip` now."""
    out = []
    if meta.get("board") != board:
        out.append(f"the backup is of {meta.get('board')!r}, not {board!r}")
    out += chip_refusals(meta, chip)
    if digest != meta.get("sha256"):
        out.append("the image's SHA-256 is not the one recorded when it was read")
    return out


def chip_refusals(meta, chip):
    """Refusals for writing a chip backup whose record is `meta` to the chip that reads as
    `chip` now: another MAC, another flash size."""
    out = []
    if (meta.get("chip") or {}).get("mac") != chip.get("mac"):
        out.append(f"the backup was read from the chip with MAC "
                   f"{(meta.get('chip') or {}).get('mac')}, this board's is {chip.get('mac')}: "
                   f"its private key would make two boards one node")
    if (meta.get("chip") or {}).get("flash_size") != chip.get("flash_size"):
        out.append(f"the backup is of a {(meta.get('chip') or {}).get('flash_size')} chip, this "
                   f"one is {chip.get('flash_size')}")
    return out
