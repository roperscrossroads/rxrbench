#!/usr/bin/env python3
"""Read, and set, the USB serial number of a classic Silicon Labs CP2102 bridge.

Classic CP2102s ship with the serial string "0001". Two boards behind such bridges cannot be
told apart by /dev/serial/by-id, so the registry cannot match them by serial. The serial is
held in the bridge's rewritable configuration memory and can be changed, and changed back.

    cp210x_serial.py show <port> [--dump FILE]
    cp210x_serial.py set  <port> <new> --expect <current> --backup FILE

<port> is the USB port path as sysfs names it (e.g. 1-1.1: `ls /sys/bus/usb/devices`).

What it will do, and nothing else (protocol: AN721; register numbers as in cp210x-program):
- read the part number (0x370B), the lock value (0x370A) and the whole 1024-byte
  configuration (0x3709); read the serial from the device's string descriptor;
- write the serial string (0x3704), only after: part number 2 (a CP2102), the lock reads
  UNLOCKED (0xFF), the current serial equals --expect, and the whole configuration has been
  saved to --backup (a new file). Afterwards the configuration is read again and must differ
  from the backup only where the new serial now is.

It never writes the lock register: locking is permanent and nothing here can do it. It does
not detach the kernel's serial driver: these are vendor requests to the device on endpoint 0,
which need no claimed interface, so the board's serial port stays up. The new serial is seen
after the bridge re-enumerates (unplug and replug). Usually needs root (usbfs write access).
"""
import argparse
import os
import re
import sys

import usb.core
import usb.util

VENDOR_IN = usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE
VENDOR_OUT = usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE
CONFIG = 0xFF
REG_SERIAL_NUMBER = 0x3704
REG_EEPROM = 0x3709
REG_LOCK_VALUE = 0x370A
REG_PART_NUMBER = 0x370B
EEPROM_SIZE = 0x400
UNLOCKED = 0xFF
LOCKED = 0xF0
CP2102 = 2
SERIAL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
TIMEOUT = 1000


class Refused(Exception):
    pass


def find(port):
    """The CP210x at sysfs port path `port` (bus-port.port...)."""
    m = re.fullmatch(r"(\d+)-(\d+(?:\.\d+)*)", port)
    if not m:
        raise Refused(f"{port!r} is not a USB port path like 1-1.1")
    bus, ports = int(m.group(1)), tuple(int(p) for p in m.group(2).split("."))
    found = [d for d in usb.core.find(find_all=True, idVendor=0x10C4, idProduct=0xEA60)
             if d.bus == bus and tuple(d.port_numbers or ()) == ports]
    if len(found) != 1:
        raise Refused(f"no CP210x (10c4:ea60) at port {port}")
    return found[0]


def read(dev, reg, length):
    return bytes(dev.ctrl_transfer(VENDOR_IN, CONFIG, reg, 0, length, TIMEOUT))


def facts(dev):
    return {
        "part": read(dev, REG_PART_NUMBER, 1)[0],
        "lock": read(dev, REG_LOCK_VALUE, 1)[0],
        "serial": usb.util.get_string(dev, dev.iSerialNumber) if dev.iSerialNumber else None,
        "product": usb.util.get_string(dev, dev.iProduct) if dev.iProduct else None,
    }


def descriptor(text):
    enc = text.encode("utf-16-le")
    return bytes([len(enc) + 2, 0x03]) + enc


def eeprom(dev):
    data = read(dev, REG_EEPROM, EEPROM_SIZE)
    if len(data) != EEPROM_SIZE:
        raise Refused(f"the configuration read returned {len(data)} B, not {EEPROM_SIZE}")
    return data


def changed_ranges(a, b):
    out, start = [], None
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y and start is None:
            start = i
        elif x == y and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(a)))
    return out


def show(args):
    dev = find(args.port)
    f = facts(dev)
    lock = {UNLOCKED: "unlocked", LOCKED: "LOCKED"}.get(f["lock"], f"unknown ({f['lock']:#04x})")
    print(f"port {args.port}: part {f['part']} ({'CP2102' if f['part'] == CP2102 else 'not a CP2102'})"
          f", {lock}, serial {f['serial']!r}, product {f['product']!r}")
    if args.dump:
        data = eeprom(dev)
        with open(args.dump, "xb") as out:
            out.write(data)
        print(f"configuration ({len(data)} B) saved to {args.dump}")


def set_serial(args):
    if not SERIAL_RE.match(args.new):
        raise Refused(f"{args.new!r}: use 1-32 of A-Z a-z 0-9 . _ -")
    dev = find(args.port)
    f = facts(dev)
    refusals = []
    if f["part"] != CP2102:
        refusals.append(f"part number {f['part']}, not a CP2102")
    if f["lock"] != UNLOCKED:
        refusals.append(f"the lock reads {f['lock']:#04x}, not unlocked ({UNLOCKED:#04x})")
    if f["serial"] != args.expect:
        refusals.append(f"the serial is {f['serial']!r}, not the expected {args.expect!r}")
    if refusals:
        raise Refused("refused: " + "; ".join(refusals))
    before = eeprom(dev)
    with open(args.backup, "xb") as out:
        out.write(before)
    os.chmod(args.backup, 0o644)
    print(f"configuration ({len(before)} B) saved to {args.backup}")

    data = descriptor(args.new)
    n = dev.ctrl_transfer(VENDOR_OUT, CONFIG, REG_SERIAL_NUMBER, 0, data, TIMEOUT)
    if n != len(data):
        raise Refused(f"short write ({n} of {len(data)} B): restore with "
                      f"`set {args.port} {args.expect} --expect <whatever it reads now>`")

    after = eeprom(dev)
    ranges = changed_ranges(before, after)
    where = after.find(data)
    if where < 0:
        raise Refused("the new serial is not in the configuration read back")
    outside = [(a, b) for a, b in ranges if a < where or b > where + len(data)]
    if outside:
        raise Refused(f"bytes changed outside the serial: {outside}; the original is in "
                      f"{args.backup}")
    print(f"written: serial {args.new!r} at configuration offset {where:#x} "
          f"({len(data)} B; changed {ranges}); lock still "
          f"{'unlocked' if read(dev, REG_LOCK_VALUE, 1)[0] == UNLOCKED else 'NOT UNLOCKED'}")
    print("unplug and replug the board to see it in the USB descriptor")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="part, lock and serial; --dump saves the configuration")
    s.add_argument("port")
    s.add_argument("--dump")
    s.set_defaults(func=show)
    w = sub.add_parser("set", help="set the serial string (refuses a locked or unexpected device)")
    w.add_argument("port")
    w.add_argument("new")
    w.add_argument("--expect", required=True, help="the serial the device must have now")
    w.add_argument("--backup", required=True, help="new file for the whole configuration")
    w.set_defaults(func=set_serial)
    args = p.parse_args(argv)
    try:
        args.func(args)
    except (Refused, usb.core.USBError, FileExistsError) as e:
        print(f"cp210x_serial: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
