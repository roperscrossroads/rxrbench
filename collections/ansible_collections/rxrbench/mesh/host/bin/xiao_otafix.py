#!/usr/bin/env python3
"""xiao_otafix.py <node> <app.uf2> [--inventory] [--carrier=<small.uf2>]
                 [--update=<bootloader.uf2> --update-sha256=<hex>] : put the OTAFIX bootloader on a XIAO, safely.

Runs on the bench host the board is plugged into, next to core's ~/rxrbench/bin/uf2_flash.py,
which it drives for every step that finds, guards or writes the drive; the caller's environment
(what asks the running app for UF2 mode: UF2_ENTERS, UF2_RUNS, UF2_ENTER_AS) is passed through to it.

  1. `uf2_flash.py uf2-mode <node>`: the board's drive, found by its chip serial.
  2. Read INFO_UF2.TXT and record it (the inventory): bootloader version, model, SoftDevice.
  3. Back up INFO_UF2.TXT and CURRENT.UF2 to ~/rxrbench/firmware/backups/.
  4. Gates, all before anything is written:
     - already OTAFIX: nothing to do;
     - anything but "Seeed XIAO nRF52840", bootloader 0.6.1, SoftDevice S140 7.3.0: STOP. The
       update file (_nosd) replaces the bootloader only and assumes that SoftDevice, and the
       staging address below was measured on 0.6.1 alone;
     - the installed image must end below 0xe0000 (with --carrier=<small.uf2>, a zephyrtastic image that
       does not is not a STOP: the carrier, which must itself end below 0xe0000, goes on first
       through the same guarded write, the update follows, and step 6 flashes the declared app;
       the carrier's stored config differs from the declared ones until `just apply`). The 0.6.1
       bootloader stages the new one at 0xe0000-0xe9900, inside a zephyrtastic image's slot0
       (0x37000-0xEC000); an image reaching it is damaged (docs/HARDWARE.md, "nRF52840 with
       a UF2 bootloader").
  5. Copy the update UF2 (sha256 checked), wait for the app to come back.
  6. `uf2_flash.py uf2 <node> <app.uf2>`: re-flashes the app through the new bootloader, which also
     prints its version. Under OTAFIX always flash by UF2, never serial DFU.

--update/--update-sha256 name a bootloader other than the pinned release (a rebuilt OTAFIX); it
replaces an OTAFIX bootloader only, and only if its own text carries the installed Model and Board-ID.
--inventory stops after step 4's report and returns the board to its app with step 6.
Whatever happens after step 1, step 6 is what brings the board back out of UF2 mode.

A stock Meshtastic board (no MCUboot; its app starts at 0x27000): pass `-` for <app.uf2>.
The image end is then the last non-blank byte of the app region, and step 6 is skipped --
the stock app boots by itself after the update, as a stock board does.
"""
import fcntl
import glob
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

UPDATE = os.path.expanduser(
    "~/rxrbench/firmware/update-xiao_nrf52840_ble_sense_bootloader-0.9.2-OTAFIX2.5_nosd.uf2")
UPDATE_SHA = "4a44832efe05ccc458bd9218934bed192aa4bd8a6f6aac5a2b45ec310c8e496d"
BACKUPS = os.path.expanduser("~/rxrbench/firmware/backups")
UF2_FLASH = os.path.expanduser("~/rxrbench/bin/uf2_flash.py")
SLOT0 = 0x37000
STAGING_LO = 0xE0000
MCUBOOT_MAGIC = 0x96F3B83D


def sh(*a, **k):
    return subprocess.run(a, capture_output=True, text=True, **k)


def uf2(*args):
    """uf2_flash.py, told this run holds the board's lock."""
    return subprocess.run([UF2_FLASH, *args], capture_output=True, text=True,
                          env={**os.environ, "UF2_LOCK_HELD": LOCKED})


def serial_of(node):
    r = uf2("resolve", node)
    m = re.search(r"([0-9A-F]{16})", r.stdout + r.stderr)
    if not m:
        sys.exit(f"cannot resolve {node}: {(r.stdout + r.stderr).strip()[-200:]}")
    return m.group(1)


def drive_mount(serial, node, wait=30):
    """The mount point of `node`'s UF2 drive, and what the drive guard established about it.
    A by-id name is only how the drive is found: uf2_flash's verify-drive asks sysfs which USB
    device is behind the mount, and refuses unless it is this board's bootloader."""
    end = time.time() + wait
    while time.time() < end:
        hits = [p for p in glob.glob("/dev/disk/by-id/usb-*") if serial in p and "-part" not in p]
        if len(hits) == 1:
            r = sh("findmnt", "-rno", "TARGET", "-S", os.path.realpath(hits[0]))
            if r.stdout.strip():
                mnt = r.stdout.strip().splitlines()[0]
                return mnt, verify_drive(node, mnt)
        time.sleep(1)
    sys.exit(f"no mounted UF2 drive for {serial} within {wait} s")


def verify_drive(node, mnt):
    r = uf2("verify-drive", node, mnt)
    if r.returncode != 0:
        sys.exit((r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip()
                 else f"the drive guard failed for {node} at {mnt}")
    return json.loads(r.stdout.strip().splitlines()[-1])


LOCKED = ""   # the serial this run holds the lock for, passed to uf2_flash so it does not retake it


def lock_dir():
    """Where this host's device locks live: devlock's directory (devlock.py `_lockdir`, the
    same rule), so every tool that locks a board looks in one place, and never in /tmp unless
    the host has no runtime dir at all."""
    explicit = os.environ.get("DEVLOCK_DIR")
    if explicit:
        d = os.path.expanduser(explicit)
    else:
        run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        d = os.path.join(run if os.path.isdir(run) else f"/tmp/devlock-{os.getuid()}", "devlock")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def lock_board(serial):
    """One run per board at a time, the lock uf2_flash takes (and honours from a parent)."""
    global LOCKED
    fh = open(os.path.join(lock_dir(), f"uf2-{serial.lower()}.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"REFUSING: another UF2 or bootloader run is already working on {serial}")
    LOCKED = serial
    return fh


def load_uf2(path):
    d = open(path, "rb").read()
    blocks = {}
    for i in range(0, len(d), 512):
        _m0, _m1, _fl, addr, n, _b, _nb, _fam = struct.unpack_from("<8I", d, i)
        blocks[addr] = d[i + 32:i + 32 + n]
    return blocks


def payload_of(path):
    """All the data bytes of a UF2 file, in file order (for what the bootloader says about itself)."""
    return b"".join(blk for _a, blk in sorted(load_uf2(path).items()))


def embedded_version(payload):
    """The version a bootloader image reports: its INFO_UF2.TXT line is in its own binary."""
    m = re.search(rb"UF2 Bootloader (\S+)", payload)
    return m.group(1).decode() if m else None


def option(name):
    for a in sys.argv[1:]:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def read(blocks, addr, n):
    out = b""
    while len(out) < n:
        base = (addr + len(out)) & ~0xFF
        blk = blocks.get(base, b"\xff" * 256)
        out += blk[(addr + len(out)) - base:]
    return out[:n]


def image_end(blocks):
    """End of the MCUboot image in slot0 (header + image + a generous TLV allowance), or None."""
    magic, _load, hdr, _ptlv, img = struct.unpack_from("<IIHHI", read(blocks, SLOT0, 16))
    if magic != MCUBOOT_MAGIC:
        return None
    return SLOT0 + hdr + img + 0x400


def stock_app_end(blocks):
    """A stock app has no header to read its size from: the end is the last non-blank byte
    between the app start (0x27000) and the top of the staging area. Stale data up there
    counts, which errs on the side of refusing."""
    used = [a for a in blocks if 0x27000 <= a < 0xEA000 and set(blocks[a]) != {0xFF}]
    return (max(used) + 256) if used else None


def decide(*, custom, ver, want, model, board, sd, payload, end, stock, carrier):
    """(go, need_carrier, lines): may the bootloader update be written now, must a small carrier
    image go on first (the installed image reaches the staging area), and what to say.
    Nothing here touches a board."""
    stop = "Nothing written."
    if custom:
        if "OTAFIX" not in ver:
            return False, False, [f"STOP: a custom bootloader only replaces an OTAFIX one (this is "
                                  f"{ver}). {stop}"]
        if want == ver:
            return False, False, [f"already {ver}, nothing to update"]
        if (f"Model: {model}".encode() not in payload
                or f"Board-ID: {board}".encode() not in payload):
            return False, False, [f"STOP: the update does not name this board (Model {model!r}, "
                                  f"Board-ID {board!r} are not in it). {stop}"]
        starting = [f"custom update: {ver} -> {want}"]
    elif "OTAFIX" in ver:
        return False, False, ["already OTAFIX, nothing to update"]
    elif not (ver == "0.6.1" and model == "Seeed XIAO nRF52840" and "S140" in sd and "7.3.0" in sd):
        return False, False, [f"STOP: not the known-good starting point (0.6.1 / Seeed XIAO "
                              f"nRF52840 / S140 7.3.0). {stop}"]
    else:
        starting = []
    if end is not None and end < STAGING_LO:
        return True, False, starting
    what = f"ends at 0x{end:x}" if end else "is not an MCUboot image"
    # The update stages in [STAGING_LO, 0xEA000), the top of slot0: an installed image that reaches
    # it is overwritten. A small carrier image can go on first, and the declared app after; only
    # for a zephyrtastic image that is there to be replaced (never a stock app, never an unknown one).
    if carrier and end is not None and not stock:
        return True, True, starting + [f"the installed image {what}; the update stages at "
                                       f"0x{STAGING_LO:x}: the carrier image goes on first"]
    return False, False, [f"STOP: the installed image {what}; the update stages at "
                          f"0x{STAGING_LO:x}. Flash a smaller image first (or give --carrier=<app.uf2>). "
                          f"{stop}"]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    inventory = "--inventory" in sys.argv
    if len(args) != 2:
        sys.exit(__doc__)
    node, app = args
    stock = app == "-"
    if not stock and not os.path.exists(app):
        sys.exit(f"no such app UF2: {app}")
    update, update_sha = UPDATE, UPDATE_SHA
    carrier = option("--carrier")
    if carrier:
        carrier = os.path.expanduser(carrier)
        if not os.path.exists(carrier):
            sys.exit(f"no such carrier UF2: {carrier}")
    custom = option("--update")
    if custom:
        # A bootloader image other than the pinned release (a rebuilt OTAFIX): named explicitly,
        # with its sha256, and held to stricter gates below.
        update = os.path.expanduser(custom)
        update_sha = option("--update-sha256")
        if not update_sha:
            sys.exit("REFUSING: --update needs --update-sha256=<hex> of the file")
    if hashlib.sha256(open(update, "rb").read()).hexdigest() != update_sha:
        sys.exit(f"REFUSING: {update} does not match the recorded sha256")
    serial = serial_of(node)
    _lock = lock_board(serial)  # noqa: F841 (held until exit)
    os.makedirs(BACKUPS, exist_ok=True)

    r = uf2("uf2-mode", node)
    print((r.stdout + r.stderr).strip().splitlines()[-1])
    mnt, drive = drive_mount(serial, node)
    print(f"{node}: drive identity confirmed by {drive['confirmed_by']} ({drive['usb']} {drive['serial']})")
    info = open(os.path.join(mnt, "INFO_UF2.TXT")).read()
    # Date and time: a second run on the same day must not overwrite the first run's "before"
    # dumps.
    stamp = time.strftime("%Y%m%dT%H%M%S")
    open(f"{BACKUPS}/{node}-INFO_UF2-before-{stamp}.TXT", "w").write(info)
    sh("cp", os.path.join(mnt, "CURRENT.UF2"), f"{BACKUPS}/{node}-CURRENT-before-{stamp}.UF2")
    # Whose is it? The file name is only what this run believed. The sidecar records the chip the
    # drive was verified to be, and the hash, so a mislabelled backup can be found later.
    verify_drive(node, mnt)
    with open(f"{BACKUPS}/{node}-CURRENT-before-{stamp}.UF2", "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    with open(f"{BACKUPS}/{node}-CURRENT-before-{stamp}.UF2.id", "w") as fh:
        json.dump({"node": node, "chip_id": serial, "sha256": digest, "drive": drive,
                   "taken": stamp, "info_uf2": info}, fh, indent=1)
    if os.path.exists(os.path.join(mnt, "NODEINFO.TXT")):
        sh("cp", os.path.join(mnt, "NODEINFO.TXT"), f"{BACKUPS}/{node}-NODEINFO-before-{stamp}.TXT")
    blocks = load_uf2(f"{BACKUPS}/{node}-CURRENT-before-{stamp}.UF2")
    end = stock_app_end(blocks) if stock else image_end(blocks)
    ver = re.search(r"UF2 Bootloader (\S+)", info).group(1)
    model = (re.search(r"Model: (.*)", info) or [None, "?"])[1].strip()
    board = (re.search(r"Board-ID: (.*)", info) or [None, "?"])[1].strip()
    sd = (re.search(r"SoftDevice: (.*)", info) or [None, "?"])[1].strip()
    date = (re.search(r"Date: (.*)", info) or [None, "?"])[1].strip()
    print(f"INVENTORY {node}: bootloader {ver} ({date}) | model {model} | board-id {board} | "
          f"softdevice {sd} | image end "
          f"{'0x%x' % end if end else 'no MCUboot image at 0x37000'}{' (stock app)' if stock else ''}")

    payload = payload_of(update) if custom else b""
    go, need_carrier, lines = decide(
        custom=bool(custom), ver=ver, want=embedded_version(payload) if custom else None,
        model=model, board=board, sd=sd, payload=payload, end=end, stock=stock, carrier=carrier)
    for line in lines:
        print(line if line.startswith("STOP") else f"{node}: {line}")
    if inventory:
        go = need_carrier = False

    if not go and not need_carrier:
        # Nothing to write: the board went to UF2 mode only to be read. It goes back to the app
        # it holds (its own app copied back by uf2_flash.py app-back), never to the declared
        # one: a run that writes no bootloader changes no firmware, stock or not.
        r = uf2("app-back", node)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0 or not ("app up" in out or "already in its app" in out):
            sys.exit(f"!! {node}: nothing was written, but it did not go back to its app from UF2 "
                     f"mode (rc {r.returncode}): " + " | ".join(out.splitlines()[-3:]))
        print(f"{node}: nothing written; its own app copied back to leave UF2 mode")
        return

    if need_carrier:
        cend = image_end(load_uf2(carrier))
        if cend is None or cend >= STAGING_LO:
            sys.exit(f"STOP {node}: the carrier {os.path.basename(carrier)} "
                     f"{'ends at 0x%x' % cend if cend else 'is not an MCUboot image'}; it must end "
                     f"below 0x{STAGING_LO:x}. Nothing written.")
        # The pre-roll backup above stays the record of what was installed. The carrier goes on
        # through the same guarded write as any app, then the board is taken back into UF2 mode.
        c = uf2("uf2", node, carrier)
        ctail = (c.stdout + c.stderr).strip().splitlines()
        if c.returncode != 0 or not any("programmed; app up" in ln for ln in ctail):
            sys.exit(f"!! {node}: the carrier image did not go on (rc {c.returncode}): "
                     + " | ".join(ctail[-3:]) + " -- nothing was written to the bootloader")
        print(f"{node}: carrier {os.path.basename(carrier)} in place (ends 0x{cend:x}); its config "
              f"differs from the declared one until the declared app and `just apply` restore them")
        time.sleep(3)
        r = uf2("uf2-mode", node)
        print((r.stdout + r.stderr).strip().splitlines()[-1])
        mnt, drive = drive_mount(serial, node)
        print(f"{node}: drive identity confirmed by {drive['confirmed_by']} "
              f"({drive['usb']} {drive['serial']})")

    if go:
        print(f"{node}: copying the OTAFIX update ({os.path.getsize(update)} B)", flush=True)
        # The backups took a while: be sure it is still this board's drive, the same mount.
        again = verify_drive(node, mnt)
        if again["st_dev"] != drive["st_dev"]:
            sys.exit(f"REFUSING: {node}'s drive is not the one that was checked first: nothing written")
        # a drive the desktop mounted for this user takes a plain copy; else sudo
        sh(*(["cp"] if os.access(mnt, os.W_OK) else ["sudo", "-n", "cp"]), update, mnt + "/")
        sh("sync")
        end_t = time.time() + 90
        up = False
        while time.time() < end_t:
            ports = [p for p in glob.glob("/dev/serial/by-id/*") if serial in p]
            if any("Zephyr" in p or (stock and "XIAO-BOOT" in p) for p in ports):
                up = True
                break
            time.sleep(1)
        if not up:
            sys.exit(f"!! {node}: the app did not come back within 90 s after the bootloader "
                     f"update. Do NOT retry blindly: check USB and BLE (XIAO_DFU) first.")
        print(f"{node}: app back after the bootloader update")
        time.sleep(3)

    if stock:
        print(f"{node}: stock app left as it is (no re-flash)")
        return
    r = uf2("uf2", node, app)
    tail = (r.stdout + r.stderr).strip().splitlines()
    for line in tail:
        if ("UF2 Bootloader" in line or "programmed" in line or "REFUSING" in line
                or "no app" in line or "drive identity" in line):
            print(line)
    after = next((ln for ln in tail if "UF2 Bootloader" in ln), "")
    if after:
        open(f"{BACKUPS}/{node}-bootloader-after-{stamp}.txt", "w").write(after + "\n")
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
