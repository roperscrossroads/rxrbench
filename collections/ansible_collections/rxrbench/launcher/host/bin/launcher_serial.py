#!/usr/bin/env python3
"""Install an app into a Launcher board over its serial console, on a bench host.

    launcher_serial.py --port /dev/ttyUSB0 --name camstr --file cam-stream.bin
                       [--offset N --length N] [--overwrite] [--wait 30] [--chunk 2048]

Launcher's console (115200, no echo, newline-ended lines) takes `flash firmware <name> <size>`
(`flash overwrite` reuses the slot of that name): it answers `READY <size>`, reads that many raw
bytes, answers `ACK <written>/<size>` for every chunk and ends with `OK flashed, rebooting`,
having created the slot, selected it in the otadata and rebooted into the app. Only the app
image goes: `--offset/--length` cut it out of a merged file.

The port is the caller's to resolve and guard by name (devreg); the broker that owns it has
released it. The board must be in Launcher, or in its boot window: the banner is answered
with `nav SelPress` when it shows. Each piece waits for its ACK before the next is sent (a
board behind a UART bridge loses what arrives while it writes flash). A late ACK is waited for
up to --ack-wait seconds; one lost on the way (a lossy USB link) is counted in `lost_acks` and the
next piece sent, since the device answers it with a count covering both; a device two pieces
behind lost bytes, and the install ends saying how far it got.

The last line printed is one JSON object: {"ok", "sent", "size", "lost_acks", "reply",
"seconds"}; exit 0 when the device said OK.
"""
import argparse
import json
import re
import sys
import time

import serial

BANNER = "Press the button to enter the Launcher"
#: an ESP32 ROM's first word after any reset: `rst:0x1 (POWERON_RESET),boot:0x13 (...)`
RESET = re.compile(r"rst:0x[0-9a-f]+ \(([A-Z0-9_]+)\)")


def resets_after_ready(log):
    """The resets the ROM reported once the install had begun (READY): the board restarted
    under it. A reset before READY is the tool's own entry into Launcher."""
    start = next((i for i, l in enumerate(log) if l.startswith("READY")), None)
    if start is None:
        return []
    return [m.group(1) for l in log[start:] for m in [RESET.search(l)] if m]


#: What the boot banner is answered with, to stay in Launcher: a key press, or bootToApp
#: turned off for this boot (Launcher merges and applies it at once; put back in the menu).
ENTRIES = {"selpress": "nav SelPress", "settings": 'settings set {"bootToApp":0}'}
MENU = "Type 'help'"


class Link:
    def __init__(self, port, baud=115200, modem_rest="asserted"):
        self.s = serial.Serial()
        self.s.port, self.s.baudrate, self.s.timeout = port, baud, 0.05
        # Where the lines rest is the board's: both asserted is a terminal at rest (deasserted
        # at open, an ESP32-S3's native USB-Serial/JTAG port straps the chip into ROM download
        # mode across every reset; a UART bridge's auto-reset circuit pulls nothing with both
        # asserted); both deasserted for a bridge whose RTS holds EN while asserted (a CH340K
        # on a handheld), which both asserted would keep in reset.
        on = modem_rest == "asserted"
        self.s.rts = on
        self.s.dtr = on
        self.s.open()
        self.buf = b""
        self.log = []
        self.pending = []      # lines read but not yet asked for: nothing the device says is dropped

    def close(self):
        self.s.close()

    def send(self, line):
        self.s.write(line.encode() + b"\n")
        self.s.flush()

    def lines(self):
        self.buf += self.s.read(8192)
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            text = line.rstrip(b"\r").decode("utf-8", "replace")
            if BANNER not in text:
                self.log.append(text)
            yield text

    def wait(self, pred, timeout):
        """The first line (kept from before, or read now) that `pred` takes; the others wait
        their turn. None when none came within `timeout`."""
        end = time.monotonic() + timeout
        while True:
            for i, line in enumerate(self.pending):
                if pred(line):
                    del self.pending[i]
                    return line
            if time.monotonic() >= end:
                return None
            new = list(self.lines())
            if new:
                self.pending = (self.pending + new)[-200:]
            else:
                time.sleep(0.01)


def enter(link, wait, entry="selpress"):
    """Launcher's console, answering: as it is, or through the boot banner (answered the
    moment it shows, before the window closes, with `ENTRIES[entry]`). After the `settings`
    answer the menu line is waited for and bootToApp put back on."""
    link.send("")
    link.send("version")
    end, asked, answered, menu = time.monotonic() + wait, time.monotonic(), False, False
    while time.monotonic() < end:
        line = link.wait(lambda l: l.startswith("Launcher ") or BANNER in l or MENU in l, 1.0)
        if line is not None and MENU in line:
            menu = True
        if line is not None and line.startswith("Launcher "):
            # the answers to what was sent here (`OK nav SelPress`, `OK settings loaded`) are
            # not the install's: nothing of them waits for the next command
            link.pending = [l for l in link.pending if not l.startswith(("OK", "ERR"))]
            if answered and entry == "settings":
                if not menu and link.wait(lambda l: MENU in l, 8.0) is None:
                    return False                       # the window did not end in the menu
                link.send('settings set {"bootToApp":1}')
                ok = link.wait(lambda l: l.startswith(("OK", "ERR")), 5.0)
                return bool(ok and ok.startswith("OK"))
            return True
        if line is not None and BANNER in line and not answered:
            link.send(ENTRIES[entry])
            answered = True
            link.pending = [l for l in link.pending if BANNER not in l]
        if line is not None or time.monotonic() - asked > 2.0:
            link.send("version")
            asked = time.monotonic()
    return False


def install(link, name, data, overwrite=False, chunk=2048, ack_wait=5.0):
    verb = "overwrite" if overwrite else "firmware"
    link.send(f"flash {verb} {name} {len(data)}")
    ready = link.wait(lambda l: l.startswith(("READY", "ERR")), 15.0)
    if not ready or not ready.startswith("READY"):
        return {"ok": False, "sent": 0, "size": len(data), "lost_acks": 0,
                "reply": ready or "no READY within 15 s"}
    # Stop and wait: the next piece goes once the device has acknowledged this one. It writes
    # each piece to flash before it reads more, and behind a UART bridge (a CH340: no flow
    # control) bytes sent meanwhile would be lost for good. An ACK line can be
    # lost too, on a lossy native-USB link (the T-Embed): when none comes within `ack_wait`
    # and only this piece is unacknowledged, the device is idle and the next piece goes (it
    # answers with a count covering both; `lost_acks` counts these). When an earlier piece is
    # still unacknowledged as well, bytes were lost on the way, and the install ends saying so.
    t0, sent, lost, acked = time.monotonic(), 0, 0, 0
    while sent < len(data):
        piece = data[sent:sent + chunk]
        link.s.write(piece)
        link.s.flush()
        sent += len(piece)
        waited = time.monotonic()
        while acked < sent:
            ack = link.wait(lambda l: l.startswith(("ACK", "ERR", "OK")), 1.0)
            if ack is None:
                if time.monotonic() - waited < ack_wait:
                    continue
                if acked >= sent - len(piece) and sent < len(data):
                    lost += 1          # this piece's ACK line went missing: go on
                    break
                return {"ok": False, "sent": sent, "size": len(data), "lost_acks": lost,
                        "acked": acked, "seconds": round(time.monotonic() - t0, 1),
                        "reply": f"no ACK for {sent} B within {ack_wait:.0f} s (the device "
                                 f"acknowledged {acked} B)"}
            if ack.startswith("ERR"):
                return {"ok": False, "sent": sent, "size": len(data), "lost_acks": lost,
                        "acked": acked, "reply": ack, "seconds": round(time.monotonic() - t0, 1)}
            if ack.startswith("OK"):
                link.pending.insert(0, ack)      # the device is done: the wait below takes it
                acked = sent = len(data)
                break
            try:
                acked = max(acked, int(ack.split()[1].split("/")[0]))
            except (IndexError, ValueError):
                pass
    done = link.wait(lambda l: l.startswith(("OK", "ERR")), 60.0)
    return {"ok": bool(done and done.startswith("OK")), "sent": sent, "size": len(data),
            "lost_acks": lost, "acked": acked, "reply": done or "no OK within 60 s",
            "seconds": round(time.monotonic() - t0, 1)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", required=True)
    ap.add_argument("--name", required=True, help="the app's name (Launcher makes the slot label of it)")
    ap.add_argument("--file", required=True)
    ap.add_argument("--offset", type=lambda x: int(x, 0), default=0, help="the app part's offset in the file")
    ap.add_argument("--length", type=lambda x: int(x, 0), default=0, help="the app part's length (0: to the end)")
    ap.add_argument("--overwrite", action="store_true", help="reuse the slot of that name")
    ap.add_argument("--wait", type=float, default=30.0, help="seconds to wait for Launcher's console")
    ap.add_argument("--chunk", type=int, default=2048)
    ap.add_argument("--ack-wait", type=float, default=5.0,
                    help="seconds to wait for each piece's ACK before giving up")
    ap.add_argument("--entry", choices=list(ENTRIES), default="selpress",
                    help="how the boot banner is answered: a key press, or bootToApp off for the boot")
    ap.add_argument("--capture", help="write every line the board printed to this file")
    ap.add_argument("--modem-rest", choices=["asserted", "deasserted"], default="asserted",
                    help="where the port's DTR and RTS rest (the board type's modem_rest)")
    a = ap.parse_args(argv)
    with open(a.file, "rb") as f:
        data = f.read()
    data = data[a.offset:a.offset + a.length] if a.length else data[a.offset:]
    if not data:
        print(json.dumps({"ok": False, "reply": "an empty app part"}))
        return 2
    link = Link(a.port, modem_rest=a.modem_rest)
    try:
        if not enter(link, a.wait, a.entry):
            print(json.dumps({"ok": False, "sent": 0, "size": len(data), "lost_acks": 0,
                              "reply": f"no Launcher console within {a.wait:.0f} s",
                              "tail": link.log[-5:]}))
            return 1
        r = install(link, a.name, data, a.overwrite, a.chunk, a.ack_wait)
        if not r["ok"]:
            # what the board said besides ACKs: a reboot banner, a panic, an ERR
            r["tail"] = [l for l in link.log if not l.startswith("ACK")][-8:]
            r["resets"] = resets_after_ready(link.log)
    finally:
        link.close()
        if a.capture:
            # everything the board said, for a failure the tail does not explain
            with open(a.capture, "w") as fh:
                fh.write("\n".join(link.log) + "\n")
    print(json.dumps(r))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
