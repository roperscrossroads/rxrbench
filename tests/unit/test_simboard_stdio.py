"""A simulated board whose firmware speaks on its standard streams (NuttX's sim): the bench
owns the pty, relays it to the firmware, keeps it across reboots."""
import os
import sys
import time

import serial

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "collections"))
from ansible_collections.rxrbench.core.sim import simboard  # noqa: E402  pylint: disable=wrong-import-position

# a firmware of three lines: a banner, then every line answered and `off` an exit
FIRMWARE = '''#!/usr/bin/env python3
import sys
sys.stdout.write("hello from firmware\\r\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if line == "off":
        break
    sys.stdout.write("you said " + line + "\\r\\n"); sys.stdout.flush()
'''


def test_the_bench_owns_the_console_of_a_firmware_on_stdio(tmp_path):
    exe = tmp_path / "fw"
    exe.write_text(FIRMWARE)
    exe.chmod(0o755)
    b = simboard.SimBoard(str(tmp_path), str(exe), name="s1", console="stdio")
    b.start()
    path = b.path()
    assert path and path.startswith("/dev/pts/"), path
    # (what the firmware said before anyone listened is gone, as on a UART)
    s = serial.Serial(path, timeout=1)
    s.write(b"ping\n")
    time.sleep(0.3)
    assert b"you said ping" in s.read(200)
    # the firmware powers itself off: the next path() boots it again, on the same console,
    # and this time someone is listening for its banner
    s.write(b"off\n")
    end = time.monotonic() + 5
    while b.proc.poll() is None and time.monotonic() < end:
        time.sleep(0.05)
    assert b.proc.poll() is not None, "the firmware did not exit"
    assert b.path() == path and b.boots == 2
    time.sleep(0.3)
    assert b"hello from firmware" in s.read(200)
    s.write(b"pong\n")
    time.sleep(0.3)
    assert b"you said pong" in s.read(200)
    s.close()
    b.stop()
