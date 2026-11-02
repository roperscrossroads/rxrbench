"""A MeshCore console on a pty, in the dialect zephcore speaks:

- each typed character is echoed, except after the command word of a secret-bearing command
  (`password `, `set guest.password `, ...), so a secret never reaches a console log;
- a line ends at CR; the answer is one line, "  -> <text>", or a bare state line
  "~S{json}*crc16" for `zephcore state`; a blank line gets no answer; an unknown one
  "  -> Unknown command";
- `zephcore key export confirm` answers one bare line "~K<base64 private key>*crc16";
  `set prv.key <128 hex>` (never echoed) takes effect at the next reboot; stock MeshCore
  (`state_line=False`) answers `get prv.key` with the key in hex, as on its serial console,
  and `erase` formats its file system: defaults and a new key at the next reboot; a few
  prefs (`lat`, `lon`, `txdelay`) are kept for `get`/`set`;
- `set radio` takes effect at the next reboot; a fresh or wiped node does not transmit until
  `zephcore tx on`; `zephcore wipe <N> keep|new` writes the epoch last and reboots (here: a
  reboot is counted and the pending prefs applied; `new` gives a new public key).
"""
import base64
import binascii
import hashlib
import json
import os
import pty
import threading
import time
import tty

SECRET_WORDS = ("password ", "set guest.password ", "set prv.key ", "set bridge.secret ")
PW_LABEL = b"zephcore/admin-password:"


def pw_fp(password):
    return f"{binascii.crc32(PW_LABEL + password.encode()) & 0xffffffff:08x}"


class FakeMeshCore:
    def __init__(self, role="repeater", family="zephcore", state_line=True, echo_secrets=False,
                 console_stays=False):
        self.role, self.family, self.state_line = role, family, state_line
        # stock MeshCore on an ESP32: the USB-Serial-JTAG console stays open across a reset and
        # the node answers again at once; only its boot banner tells the reboot
        self.console_stays = console_stays
        # stock MeshCore answers a blank line with "Unknown command" (None: as zephcore, not)
        self.blank_delay = None
        self.answer_delay = 0.0
        self.neighbours = []   # "<pubkey prefix>:<secs ago>:<snr>" lines for `neighbors`
        # A firmware that does not keep the contract: it echoes a secret like any other text,
        # and repeats it in its answer.
        self.echo_secrets = echo_secrets
        self.st = {"name": "zephcore", "radio": [910.525, 62.5, 7, 5], "tx_dbm": 10,
                   "tx_enabled": False, "epoch": 0, "password": "",
                   "advert_min": 0, "flood_h": 47, "prefs": {"lat": "0.0", "lon": "0.0",
                                                             "txdelay": "0.5"}}
        self.pending_erase = False
        self.set_key(bytes([0x11]) * 64)
        self.pending_radio = None
        self.pending_prv = None
        self.reboots = 0
        self.uptime = 1          # grows with every state read, back to 1 at a reboot
        self.wipes = []
        # A stock node has no uptime to tell a reboot by; its USB console goes away while it
        # reboots (here: it answers nothing until this time).
        self.down_until = 0.0
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self._slave = slave
        self.seen = []
        self.line = ""
        self.echo = True
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    # -- console ---------------------------------------------------------------------
    def out(self, text):
        os.write(self.master, text.encode())

    def _run(self):
        os.set_blocking(self.master, False)
        while not self.stop:
            try:
                data = os.read(self.master, 4096)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            except OSError:
                return
            for ch in data.decode(errors="replace"):
                if ch == "\r":
                    self.out("\n")
                    line, self.line, self.echo = self.line, "", True
                    if line.strip() and time.monotonic() < self.down_until:
                        continue
                    if not line.strip() and not self.state_line and self.blank_delay is not None:
                        # stock MeshCore answers a blank line too, here a little late
                        threading.Timer(self.blank_delay, self.out,
                                        ["  -> Unknown command\r\n"]).start()
                        continue
                    if line.strip():
                        self.seen.append(line)
                        reply = self._execute(line.strip())
                        if self.echo_secrets and reply == "  -> OK" and line.startswith("password "):
                            reply = "  -> OK password is " + line.strip().split(" ", 1)[1]
                        if reply is not None:
                            if self.answer_delay:
                                time.sleep(self.answer_delay)   # a busy node, slow to answer
                            self.out(reply + "\r\n")
                elif ch == "\n":
                    continue
                else:
                    self.line += ch
                    if self.echo:
                        self.out(ch)
                    if any(self.line == w for w in SECRET_WORDS) and not self.echo_secrets:
                        self.echo = False

    # -- the node ----------------------------------------------------------------------
    def set_key(self, prv):
        """The identity: here the public key is a hash of the private one (the real firmware
        derives it with Ed25519); what matters is that it follows the private key."""
        self.st["prv"] = prv
        self.st["pubkey"] = hashlib.sha256(prv).hexdigest()

    def state(self):
        r = self.st["radio"]
        d = {"v": 1, "family": self.family, "version": "0.1.0", "build": "abcdef01",
             "board": "native_sim", "board_tag": "native-sim", "role": self.role,
             "pubkey": self.st["pubkey"],
             "pubkey_fp": f"{binascii.crc32(bytes.fromhex(self.st['pubkey'])) & 0xffffffff:08x}",
             "name": self.st["name"],
             "radio": {"freq_mhz": r[0], "bw_khz": r[1], "sf": r[2], "cr": r[3],
                       "tx_dbm": self.st["tx_dbm"], "tx_enabled": self.st["tx_enabled"]},
             "storage": {"fs": "ok", "epoch": self.st["epoch"], "used": 20480, "total": 122880},
             "uptime_s": self.uptime}
        self.uptime += 10
        if self.role == "repeater" and self.st["password"]:
            d["pw_fp"] = pw_fp(self.st["password"])
        j = json.dumps(d, separators=(",", ":"))
        return f"~S{j}*{binascii.crc_hqx(j.encode(), 0):04x}"

    def reboot(self):
        self.reboots += 1
        self.uptime = 1
        if not self.state_line and not self.console_stays:
            self.down_until = time.monotonic() + 4.0   # past a 2 s command deadline
        if self.pending_erase:   # stock `erase`: the file system went; defaults, a new key
            self.pending_erase = False
            self.st.update(name="zephcore", tx_dbm=10, password="", advert_min=0, flood_h=47,
                           radio=[915.0, 250.0, 10, 5],
                           prefs={"lat": "0.0", "lon": "0.0", "txdelay": "0.5"})
            self.set_key(hashlib.sha256(os.urandom(8)).digest() * 2)
        if self.console_stays:
            self.out("ESP-ROM:esp32s3-20210327\r\nrst:0xc (RTC_SW_CPU_RST),boot:0x28 "
                     "(SPI_FAST_FLASH_BOOT)\r\nRepeater ID: " + self.st["pubkey"].upper() + "\r\n")
        if self.pending_radio:
            self.st["radio"], self.pending_radio = self.pending_radio, None
        if self.pending_prv:
            self.set_key(self.pending_prv)
            self.pending_prv = None

    def _execute(self, line):
        w = line.split()
        st = self.st
        if line == "zephcore state":
            return self.state() if self.state_line else "  -> Unknown command"
        if line == "zephcore epoch":
            return f"  -> epoch {st['epoch']}"
        if w[:2] == ["zephcore", "tx"]:
            if len(w) == 3:
                st["tx_enabled"] = w[2] == "on"
            return f"  -> tx {'on' if st['tx_enabled'] else 'off'}"
        if w[:2] == ["zephcore", "wipe"] and len(w) == 4 and w[3] in ("keep", "new"):
            self.wipes.append((int(w[2]), w[3]))
            st.update(name="zephcore", tx_dbm=10, tx_enabled=False, password="",
                      advert_min=0, flood_h=47, radio=[910.525, 62.5, 7, 5])
            if w[3] == "new":
                self.set_key(bytes([0x22]) * 64)
            st["epoch"] = int(w[2])
            self.pending_radio = None
            self.reboot()
            return None                       # the node reboots; no answer
        if line == "reboot":
            self.reboot()
            return None
        if line == "neighbors":
            # one line per neighbour inside one answer, joined by a bare LF, as MeshCore prints it
            return "  -> " + ("\n".join(self.neighbours) if self.neighbours else "-none-")
        if line == "erase" and not self.state_line:
            self.pending_erase = True
            return "  -> File system erase: OK"
        if line == "ver":
            return "  -> v1.17.1 (Build: 14 Aug 2026)"
        if w[0] == "get" and len(w) == 2:
            v = {"name": st["name"], "tx": st["tx_dbm"], "role": self.role,
                 "public.key": st["pubkey"],
                 "radio": "{:.7f},{},{},{}".format(*st["radio"]),
                 "advert.interval": st["advert_min"], "flood.advert.interval": st["flood_h"]}
            if w[1] == "prv.key" and not self.state_line:
                return f"  -> > {st['prv'].hex().upper()}"   # stock: serial console only
            if w[1] in ("prv.key", "guest.password"):
                return "  -> Error: refused, that is a secret"
            if w[1] in st["prefs"]:
                return f"  -> > {st['prefs'][w[1]]}"
            return f"  -> > {v[w[1]]}" if w[1] in v else "  -> Unknown command"
        if w[0] == "set" and len(w) >= 3:
            key, val = w[1], line.split(None, 2)[2]
            if key == "name":
                st["name"] = val
                return "  -> OK"
            if key == "tx":
                st["tx_dbm"] = int(val)
                return "  -> OK"
            if key == "radio":
                f, bw, sf, cr = val.split(",")
                self.pending_radio = [float(f), float(bw), int(sf), int(cr)]
                return "  -> OK - reboot to apply"
            if key == "advert.interval":
                st["advert_min"] = int(val)
                return "  -> OK"
            if key == "flood.advert.interval":
                st["flood_h"] = int(val)
                return "  -> OK"
            if key in st["prefs"]:
                st["prefs"][key] = val
                return "  -> OK"
        if line == "zephcore key export":
            return "  -> Error: refused. This prints the private key; add `confirm`"
        if line == "zephcore key export confirm":
            b64 = base64.b64encode(st["prv"]).decode()
            return f"~K{b64}*{binascii.crc_hqx(b64.encode(), 0):04x}"
        if w[:2] == ["set", "prv.key"] and len(w) == 3:
            try:
                prv = bytes.fromhex(w[2])
            except ValueError:
                return "  -> Error: bad key"
            if len(prv) != 64:
                return "  -> Error: bad key"
            self.pending_prv = prv
            if not self.state_line:   # stock MeshCore's answer
                return f"  -> OK, reboot to apply! New pubkey: {st['pubkey'][:8]}"
            return "  -> OK - reboot to apply"
        if w[0] == "password" and len(w) == 2:
            st["password"] = w[1]
            if not self.state_line:   # stock MeshCore repeats it, "to let admin know for sure"
                return f"  -> password now: {w[1]}"
            return "  -> OK"
        return "  -> Unknown command"

    def close(self):
        self.stop = True
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass
