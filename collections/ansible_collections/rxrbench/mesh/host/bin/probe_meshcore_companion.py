#!/usr/bin/env python3
"""Survey probe: what a stock MeshCore companion says of itself over its USB frame protocol,
read-only.

Usage: probe_meshcore_companion.py <port>. Prints one JSON object. Run by rxrbench's survey on
a bench host (usb_survey), in a child process so a port that never answers is killed. A
companion build speaks no text CLI on USB: frames, host to device `<` + length (2 bytes, little
endian) + payload, device to host `>` + length + payload. It is asked CMD_DEVICE_QUERY (22, with
the protocol version the asker understands) and answers RESP_CODE_DEVICE_INFO (13): its version
code, contact and channel limits, its BLE PIN (not kept), a build date (12 bytes), the
manufacturer's model (40) and its version (20). Nothing is written to the node.
"""
import json
import sys
import time

CMD_DEVICE_QUERY, RESP_CODE_DEVICE_INFO, ASKS_VERSION = 22, 13, 3


def frame(payload):
    return b"<" + len(payload).to_bytes(2, "little") + payload


def frames(buf):
    """Complete device frames in `buf` (whatever else the port printed is skipped), and the rest."""
    out = []
    while True:
        i = buf.find(b">")
        if i < 0 or len(buf) < i + 3:
            return out, buf[i:] if i >= 0 else b""
        n = int.from_bytes(buf[i + 1:i + 3], "little")
        if len(buf) < i + 3 + n:
            return out, buf[i:]
        out.append(buf[i + 3:i + 3 + n])
        buf = buf[i + 3 + n:]


def text(raw):
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def device_info(payload):
    """The fields of a RESP_CODE_DEVICE_INFO frame, or None when it is not one."""
    if len(payload) < 80 or payload[0] != RESP_CODE_DEVICE_INFO:
        return None
    version = text(payload[60:80])
    return {"role": "companion", "fw_ver_code": payload[1], "build_date": text(payload[8:20]),
            "model": text(payload[20:60]), "version": version.split("-")[0] or None,
            "build": version.split("-")[1] if "-" in version else None}


def probe(port, timeout=4.0, serial_open=None):
    if serial_open is None:
        import serial  # pylint: disable=import-outside-toplevel

        def serial_open(path):
            return serial.Serial(path, 115200, timeout=0.2, exclusive=True)
    with serial_open(port) as s:
        s.reset_input_buffer()
        s.write(frame(bytes([CMD_DEVICE_QUERY, ASKS_VERSION])))
        buf, end = b"", time.monotonic() + timeout
        while time.monotonic() < end:
            buf += s.read(512)
            got, buf = frames(buf)
            for f in got:
                info = device_info(f)
                if info:
                    return info
    return None


def main(argv):
    info = probe(argv[1])
    # nothing came back: not a companion, which is no failure (the survey asks the next family)
    print(json.dumps(info or {}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
