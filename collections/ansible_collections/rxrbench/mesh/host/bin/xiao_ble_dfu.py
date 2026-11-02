#!/usr/bin/env python3
"""xiao_ble_dfu.py <ble-addr> <package.zip> — flash a XIAO over BLE through its
bootloader's Nordic legacy DFU service (the OTAFIX bootloader's `XIAO_DFU` mode).

The OTAFIX 2.5 bootloader with no valid app to boot sits in BLE DFU mode and does not
enumerate on USB (docs/HARDWARE.md, "nRF52840 with a UF2 bootloader"). This flashes it
from a BLE host with nobody at the board: it speaks the Nordic SDK 11 legacy DFU protocol,
which the Adafruit-lineage bootloaders speak over BLE, with the same package
`adafruit-nrfutil dfu genpkg` builds for serial DFU. A failed run leaves the board in DFU mode,
so it can be retried.

The protocol, in order (control point = CP, 0x1531; packet char = 0x1532):
  1. notify on CP; write CP [START_DFU, APPLICATION]; write packet: 12 bytes of
     image sizes <sd, bl, app>; wait for [RESPONSE, START_DFU, SUCCESS]. The
     bootloader erases the app region before answering, so this wait is long.
  2. write CP [INIT, RECEIVE]; the init packet (.dat) to packet; CP [INIT,
     COMPLETE]; wait for [RESPONSE, INIT, SUCCESS].
  3. write CP [PRN, n]: a receipt notification every n packets -- the flow
     control, since the packets themselves are write-without-response.
  4. write CP [RECEIVE_FIRMWARE]; stream the .bin in 20-byte packets, pausing
     at every receipt; wait for [RESPONSE, RECEIVE_FIRMWARE, SUCCESS].
  5. CP [VALIDATE] -> SUCCESS; CP [ACTIVATE_AND_RESET]. The board boots the new
     app (for a zephyrtastic image: MCUboot at 0x27000, then the app).

Throughput is about 3.4 KB/s (a 776 KB image in about 4 minutes). After the activate, expect
the board to need a reset or a replug before it shows on USB or BLE; never cycle hub power to
get one (docs/HARDWARE.md, "USB identity and hubs").

Run it under the host's ~/.venv-mcumgr python (bleak). It refuses to start unless
the target advertises the legacy DFU service, so it cannot be pointed at a
running node's app by mistake.
"""
import asyncio
import json
import struct
import sys
import time
import zipfile

from bleak import BleakClient, BleakScanner

DFU_SERVICE = "00001530-1212-efde-1523-785feabcd123"
DFU_CP = "00001531-1212-efde-1523-785feabcd123"
DFU_PACKET = "00001532-1212-efde-1523-785feabcd123"

START_DFU, INIT_DFU, RECEIVE_FW, VALIDATE, ACTIVATE_RESET = 0x01, 0x02, 0x03, 0x04, 0x05
PRN_REQ, RESPONSE, PKT_RECEIPT = 0x08, 0x10, 0x11
IMG_APPLICATION = 0x04
STATUS = {1: "SUCCESS", 2: "INVALID_STATE", 3: "NOT_SUPPORTED", 4: "DATA_SIZE_EXCEEDS_LIMIT",
          5: "CRC_ERROR", 6: "OPERATION_FAILED"}
PKT = 20
PRN = 10


def load_package(path):
    with zipfile.ZipFile(path) as z:
        man = json.loads(z.read("manifest.json"))["manifest"]
        app = man.get("application")
        if not app:
            sys.exit(f"{path}: no application in the manifest (only app packages are supported)")
        return z.read(app["bin_file"]), z.read(app["dat_file"])


async def run(addr, pkg):
    firmware, init = load_package(pkg)
    print(f"package: application {len(firmware)} B, init packet {len(init)} B")

    dev = await BleakScanner.find_device_by_address(addr, timeout=20)
    if dev is None:
        sys.exit(f"{addr} not seen advertising -- is it in DFU mode (XIAO_DFU)?")

    q: asyncio.Queue = asyncio.Queue()

    async with BleakClient(dev, timeout=30) as c:
        uuids = [s.uuid.lower() for s in c.services]
        if DFU_SERVICE not in uuids:
            sys.exit(f"REFUSING: {addr} ({dev.name!r}) has no legacy DFU service -- "
                     "not a bootloader in BLE DFU mode")
        print(f"connected: {addr} ({dev.name!r}), legacy DFU service present")
        await c.start_notify(DFU_CP, lambda _s, d: q.put_nowait(bytes(d)))

        async def expect(op, timeout):
            while True:
                d = await asyncio.wait_for(q.get(), timeout)
                if d[0] == RESPONSE and d[1] == op:
                    if d[2] != 1:
                        sys.exit(f"op 0x{op:02x} failed: {STATUS.get(d[2], d[2])}")
                    return
                # stray receipts from the previous phase are harmless here

        async def cp(data):
            await c.write_gatt_char(DFU_CP, bytes(data), response=True)

        async def pkt(data):
            await c.write_gatt_char(DFU_PACKET, bytes(data), response=False)

        print("start DFU (the bootloader erases the app region first -- this wait is long)")
        await cp([START_DFU, IMG_APPLICATION])
        await pkt(struct.pack("<III", 0, 0, len(firmware)))
        await expect(START_DFU, 120)

        print("init packet")
        await cp([INIT_DFU, 0x00])
        for i in range(0, len(init), PKT):
            await pkt(init[i:i + PKT])
        await cp([INIT_DFU, 0x01])
        await expect(INIT_DFU, 30)

        await cp([PRN_REQ, PRN & 0xFF, PRN >> 8])
        await cp([RECEIVE_FW])
        t0 = time.monotonic()
        sent = 0
        n = 0
        last_pct = -1
        while sent < len(firmware):
            chunk = firmware[sent:sent + PKT]
            await pkt(chunk)
            sent += len(chunk)
            n += 1
            if n % PRN == 0 and sent < len(firmware):
                while True:
                    d = await asyncio.wait_for(q.get(), 30)
                    if d[0] == PKT_RECEIPT:
                        got = struct.unpack_from("<I", d, 1)[0]
                        if got != sent:
                            sys.exit(f"receipt says {got} B received, sent {sent} B")
                        break
                    if d[0] == RESPONSE:
                        sys.exit(f"unexpected response mid-transfer: {d.hex()}")
            pct = sent * 100 // len(firmware)
            if pct != last_pct and pct % 10 == 0:
                rate = sent / max(time.monotonic() - t0, 1e-3) / 1024
                print(f"  {pct:3d}%  {sent}/{len(firmware)} B  {rate:.1f} KB/s", flush=True)
                last_pct = pct
        await expect(RECEIVE_FW, 60)
        print(f"transfer complete in {time.monotonic() - t0:.0f} s")

        await cp([VALIDATE])
        await expect(VALIDATE, 60)
        print("validated; activating (the board resets into the new image)")
        try:
            await cp([ACTIVATE_RESET])
        except Exception as e:  # the link drops as the board resets -- expected
            print(f"  (link dropped on reset: {type(e).__name__}, expected)")
    print("done")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(run(sys.argv[1], sys.argv[2]))
