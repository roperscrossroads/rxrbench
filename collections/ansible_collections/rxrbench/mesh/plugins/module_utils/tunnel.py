"""The phone API through a board's shell: `meshtastic api`.

A zephyrtastic node's USB port is the shell and the log, so the stock binary serial framing cannot
go there. The firmware carries the same ToRadio / FromRadio frames as base64 in shell commands
and output lines instead (its `src/meshtastic_shell_api.c` is the specification):

    meshtastic api                    serve what is waiting
    meshtastic api + <base64>         part of a ToRadio, more to come
    meshtastic api <base64> <crc16>   the last part; CRC-16/XMODEM over the whole ToRadio

    ~P<base64 FromRadio>*<crc16>      one frame; the CRC is over the base64 text
    ~P.<n> | ~P+<n>                   n frames served; nothing more | more waiting
    ~P:<bytes>                        part stored
    ~P!<why>                          refused: args, b64, len, crc, busy

Three layers, each usable alone:

- `commands()` / `parse()`: the text framing, no I/O;
- `Tunnel`: one request and its frames, over any `run(cmd) -> output` (a broker's `exec`);
- `TunnelInterface`: the stock `meshtastic` library's interface over a `Tunnel`, so everything
  that library can do to a stock node over USB it can do to this one.
"""
import base64
import binascii
import queue
import re
import threading
import time

try:
    from meshtastic.mesh_interface import MeshInterface
    HAS_MESHTASTIC = True
except ImportError:  # the framing and Tunnel work without the library
    MeshInterface = object
    HAS_MESHTASTIC = False

COMMAND = "meshtastic api"
# Raw bytes per command: 200 characters of base64, which with the command words stays under
# the shell's 256-character line. A multiple of 3, so every part is base64 by itself.
PART = 150
FRAME_MAX = 512
# A refusal that a second attempt can cure: a byte lost on the console, or the node busy.
RETRYABLE = ("crc", "b64", "busy")

_FRAME = re.compile(r"^~P([A-Za-z0-9+/]+={0,2})\*([0-9a-f]{4})$")
_STATUS = re.compile(r"^~P([.+:])(\d+)$")
_REFUSED = re.compile(r"^~P!(\w+)$")


def _described(line):
    """A line for an error message: a frame's payload may hold a private key, so a line that
    starts like one is described by its length only."""
    return f"{len(line)} chars" if line.startswith("~P") else repr(line[:40])


class TunnelError(Exception):
    pass


class Refused(TunnelError):
    """The node answered `~P!<why>`."""

    def __init__(self, why):
        super().__init__(f"the node refused the frame: {why}")
        self.why = why


def crc16(data):
    return binascii.crc_hqx(bytes(data), 0)


def commands(toradio, part=PART):
    """The shell commands that carry one serialized ToRadio."""
    toradio = bytes(toradio)
    if not 0 < len(toradio) <= FRAME_MAX:
        raise TunnelError(f"a ToRadio of {len(toradio)} bytes cannot be sent (1..{FRAME_MAX})")
    if part <= 0 or part % 3:
        raise ValueError("part must be a positive multiple of 3")
    parts = [toradio[i:i + part] for i in range(0, len(toradio), part)]
    out = [f"{COMMAND} + {base64.b64encode(p).decode()}" for p in parts[:-1]]
    out.append(f"{COMMAND} {base64.b64encode(parts[-1]).decode()} {crc16(toradio):04x}")
    return out


class Reply:
    """One command's answer: `frames` (serialized FromRadio), `status` and its number."""

    def __init__(self, frames, status, n):
        self.frames, self.status, self.n = frames, status, n

    @property
    def more(self):
        return self.status == "+"


def parse(output):
    """Read one command's output. Raises unless it is whole: every `~P` line well formed and
    checksummed, exactly one status line, last, counting the frames in front of it."""
    frames, status = [], None
    for line in output.splitlines():
        # The sentinel may sit behind a prompt or colour codes; it is never inside a log line.
        at = line.find("~P")
        if at < 0:
            continue
        line = line[at:].strip()
        if status is not None:
            raise TunnelError(f"output after the status line ({_described(line)})")
        m = _REFUSED.match(line)
        if m:
            raise Refused(m.group(1))
        m = _STATUS.match(line)
        if m:
            status = (m.group(1), int(m.group(2)))
            continue
        m = _FRAME.match(line)
        if not m:
            raise TunnelError(f"a damaged line ({_described(line)})")
        if crc16(m.group(1).encode()) != int(m.group(2), 16):
            raise TunnelError("a frame's checksum does not match")
        frames.append(base64.b64decode(m.group(1), validate=True))
    if status is None:
        raise TunnelError("no status line: the answer is incomplete, or the firmware has no "
                          "`meshtastic api`")
    if status[0] in ".+" and status[1] != len(frames):
        raise TunnelError(f"the node served {status[1]} frames, {len(frames)} arrived")
    return Reply(frames, status[0], status[1])


class Tunnel:
    """Requests over `run(cmd) -> output`. Not thread-safe: one console, one command at a time."""

    def __init__(self, run, part=PART, retries=2):
        self.run, self.part, self.retries = run, part, retries

    def _send_once(self, toradio):
        cmds = commands(toradio, self.part)
        held = 0
        for cmd in cmds[:-1]:
            reply = parse(self.run(cmd))
            held += self.part
            if reply.status != ":" or reply.n != held:
                raise TunnelError(f"part not stored: ~P{reply.status}{reply.n}, expected "
                                  f"~P:{held}")
        return parse(self.run(cmds[-1]))

    def send(self, toradio):
        """Send one ToRadio; return the Reply (the frames it drew, and whether more wait)."""
        last = None
        for _attempt in range(1 + self.retries):
            try:
                return self._send_once(toradio)
            except Refused as e:
                # A refusal drops whatever the node held, so the whole frame goes again.
                if e.why not in RETRYABLE:
                    raise
                last = e
                time.sleep(0.2 if e.why == "busy" else 0)
        raise last

    def poll(self):
        """Serve what is waiting."""
        return parse(self.run(COMMAND))

    def drain(self, limit=1000):
        """Every waiting frame, across as many polls as it takes."""
        frames = []
        for _ in range(limit):
            reply = self.poll()
            frames += reply.frames
            if not reply.more:
                return frames
        raise TunnelError("the node never ran out of frames")


class TunnelInterface(MeshInterface):
    """The stock library's interface to a node, through `run(cmd) -> output`.

        with broker_client.Board("board1") as b:
            iface = TunnelInterface(b.exec)
            iface.localNode.setOwner("board1")
            iface.close()

    The node cannot push: frames come as answers to commands. A send returns the frames
    it drew; a reader thread polls for the rest and hands every frame to the library, in the
    order the node served them.
    """

    def __init__(self, run, noNodes=False, timeout=300, poll_interval=0.5,
                 connect_timeout=60.0, part=PART):
        if not HAS_MESHTASTIC:
            raise TunnelError("the `meshtastic` Python library is not installed")
        self._tunnel = Tunnel(run, part=part)
        self._console = threading.Lock()   # one command on the console at a time
        self._inbox = queue.Queue()
        self._wake = threading.Event()
        self._more = False
        self._want = True
        self._damaged = 0           # polls whose answer had a damaged line, asked again
        self._interval = poll_interval
        MeshInterface.__init__(self, debugOut=None, noProto=False, noNodes=noNodes,
                               timeout=timeout)
        self._reader = threading.Thread(target=self._read_loop, name="TunnelReader",
                                        daemon=True)
        self._reader.start()
        try:
            self._startConfig()
            self._waitConnected(timeout=connect_timeout)
            self.waitForConfig()
        except Exception:
            self._stop_reader()
            raise

    def _take(self, reply):
        for frame in reply.frames:
            self._inbox.put(frame)
        self._more = reply.more
        self._wake.set()

    def _sendToRadioImpl(self, toRadio):
        data = toRadio.SerializeToString()
        if not data or not self._want:
            return
        with self._console:
            self._take(self._tunnel.send(data))

    def _read_loop(self):
        next_poll = time.monotonic() + self._interval
        while self._want:
            self._wake.wait(max(0.0, next_poll - time.monotonic()))
            self._wake.clear()
            try:
                # Frames a send drew go up first; they are older than anything a poll finds.
                while True:
                    try:
                        frame = self._inbox.get_nowait()
                    except queue.Empty:
                        break
                    self._handleFromRadio(frame)
                if self._want and (self._more or time.monotonic() >= next_poll):
                    again = False
                    with self._console:
                        try:
                            self._take(self._tunnel.poll())
                        except TunnelError as e:
                            # One damaged line in a poll's answer loses the frames of that
                            # poll, which a read can ask for again: before the node's config
                            # has arrived whole, the config is asked for afresh (the node
                            # streams it again, the library takes it again); after, the
                            # caller's own request times out and asks. Twice, then it is a
                            # console to look at.
                            if "damaged" not in str(e) or self._damaged >= 2:
                                raise
                            self._damaged += 1
                            again = not self.isConnected.is_set()
                    if again:
                        self._startConfig()      # takes the console itself
                    next_poll = time.monotonic() + self._interval
            except Exception as e:  # pylint: disable=broad-except
                # A waiter (connect, waitForConfig) raises this instead of timing out.
                self.failure = e
                self._want = False
                self.isConnected.set()
                return

    def _stop_reader(self):
        self._want = False
        self._wake.set()
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=5)

    def close(self):
        if self._want:
            try:
                MeshInterface.close(self)   # heartbeat off, ToRadio.disconnect
            finally:
                self._stop_reader()
                self._disconnected()
