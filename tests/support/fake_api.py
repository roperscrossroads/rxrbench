"""The firmware's side of `meshtastic api`, as a `run(cmd) -> output` callable.

A model of zephyrtastic's src/meshtastic_shell_api.c: parts are assembled, the last one is
checked against its CRC, the ToRadio goes to `handle`, and waiting FromRadio frames are served
`burst` at a time behind `~P` with a checksum. Hooks let a test damage either direction once.
"""
import base64
import binascii
import collections

FRAME_MAX = 512


def crc16(data):
    return binascii.crc_hqx(bytes(data), 0)


class FakeApi:
    def __init__(self, handle=None, burst=4):
        self.handle = handle or (lambda toradio: [])
        self.burst = burst
        self.held = b""
        self.waiting = collections.deque()   # serialized FromRadio
        self.received = []                   # every ToRadio that passed its CRC
        self.commands = []                   # every command line, as typed
        self.mangle_in = None                # f(cmd) -> cmd, applied once
        self.mangle_out = None               # f(output) -> output, applied once
        self.busy = 0                        # answer ~P!busy this many times

    def push(self, fromradio):
        """A frame arriving by itself (mesh traffic), to be found by a poll."""
        self.waiting.append(bytes(fromradio))

    def __call__(self, cmd, deadline=None):  # pylint: disable=unused-argument
        if self.mangle_in:
            cmd, self.mangle_in = self.mangle_in(cmd), None
        self.commands.append(cmd)
        out = self._execute(cmd.split())
        if self.mangle_out:
            out, self.mangle_out = self.mangle_out(out), None
        return out

    def _refuse(self, why):
        self.held = b""
        return f"~P!{why}"

    def _append(self, text):
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            return "b64"
        if not data:
            return "b64"
        if len(self.held) + len(data) > FRAME_MAX:
            return "len"
        self.held += data
        return None

    def _execute(self, argv):
        if argv[:2] != ["meshtastic", "api"]:
            return f"{argv[0]}: command not found"
        if self.busy:
            self.busy -= 1
            return "~P!busy"
        args = argv[2:]
        if len(args) == 2 and args[0] == "+":
            why = self._append(args[1])
            return self._refuse(why) if why else f"~P:{len(self.held)}"
        if len(args) == 2:
            why = self._append(args[0])
            if why:
                return self._refuse(why)
            try:
                want = int(args[1], 16)
            except ValueError:
                return self._refuse("crc")
            if want != crc16(self.held):
                return self._refuse("crc")
            toradio, self.held = self.held, b""
            self.received.append(toradio)
            self.waiting.extend(bytes(f) for f in self.handle(toradio))
        elif args:
            return self._refuse("args")
        self.held = b""
        lines = []
        while self.waiting and len(lines) < self.burst:
            text = base64.b64encode(self.waiting.popleft()).decode()
            lines.append(f"~P{text}*{crc16(text.encode()):04x}")
        lines.append(f"~P{'+' if self.waiting else '.'}{len(lines)}")
        return "\n".join(lines)
