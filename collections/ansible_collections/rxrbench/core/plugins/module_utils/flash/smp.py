"""The flasher for a Zephyr device with MCUmgr (SMP) and MCUboot: an update, not a reflash.

The image is uploaded into the spare slot while the application runs, marked for a TEST
boot, and the device is reset. MCUboot swaps the slots. If the new image comes up, it is
confirmed; if it does not, MCUboot swaps back at the next reset and the device runs what it
ran before. Done with the `smpclient` library, not by reading a CLI's output.

Identity is the image's hash: MCUboot's SHA-256 TLV is what MCUmgr reports for a slot, so
"does the device run this image" is one comparison (checked on hardware).

Transports: `serial` (MCUmgr over the shell's UART: the console has to be free, so the
board's broker releases it for each session), `ble`, and `udp` (a board on a network: the
route's `ip` and `port`, 1337 by default, guarded against the registry as a BLE address is;
nothing to lock and no bond). Needs `smpclient` in the module's
Python (`~/.venv-mcumgr` on the bench hosts).

BLE, three things the serial path has no need of (docs/HARDWARE.md has the reasons):

- **The host's one adapter is locked for every session** (devlock, resource `ble:<adapter>`).
  A busy adapter is waited for up to `ble_lock_wait` seconds, then refused naming the holder.
- **A stale bond is repaired only on the board's word.** When the first exchange fails on a
  device BlueZ shows as paired, the board's console says which bonds it holds (`console`, the
  board's broker). Only when it holds none for this host is the host's bond removed and the
  read tried once more. When the board holds the bond too, or cannot be asked, nothing is
  removed and the error says which. `rebond: never` turns this off; `rebond: force` removes
  the bond without asking (a device with no console), except after a failure to connect.
- **The console after the swap**: with `console`, verify waits for the board's console to
  answer as well as MCUmgr, which answers first.
"""
import asyncio
import os
import re
import sys
import time

from .. import broker_client, familyspec, identity, stateline
from . import common

IMAGE_FILE = "app.signed.bin"   # beside the manifest, whatever the wired format is
BLE_CONNECT_TRIES = 2
#: Seconds one connect may take: scan, link, service discovery. smpclient caps all of it at the
#: client's request timeout unless told otherwise, and on a host whose controller aborts a few
#: LE connects before one holds, the 10 s that leaves is spent before the link holds.
BLE_CONNECT_S = 30.0
BLE_ADAPTER = "hci0"
BLE_LOCK_WAIT = 300.0           # a flash over BLE takes ~200 s; one queued ahead is the usual wait
UDP_PORT = 1337                 # MCUmgr's UDP port (Zephyr's CONFIG_MCUMGR_TRANSPORT_UDP_PORT)
#: The network bearers a udp transport goes over (the registry's reach.<bearer>).
UDP_BEARERS = ("eth", "wifi")


class ConnectError(common.Unanswered):
    """The transport could not connect (scan, link, service discovery): nothing was exchanged,
    so the failure itself says nothing about keys, and the board counts as not answering. A scan that timed out looks like this, and
    so does a stale bond: BlueZ starts encryption on the key it holds as soon as the link is
    up, the device has no such key and drops the link, and service discovery fails
    ("failed to discover services, device disconnected"). Only the board can say which."""


def _newer(running, candidate):
    """The running image's version ranks above the candidate's, by the numbers MCUboot
    compares (major.minor.revision, then the build counter); unknown versions do not."""
    def nums(v):
        return [int(x) for x in re.findall(r"\d+", str(v or ""))[:4]]
    a, b = nums(running), nums(candidate)
    return bool(a) and bool(b) and a > b


def _smp_session_errors():
    """The exception classes smpclient raises inside a session (none of them an OSError):
    the transport's "disconnected from <addr>" and the client's own (sequence, upload,
    validation). An empty tuple where the library is absent (the unit tests)."""
    classes = []
    try:
        from smpclient.transport import SMPTransportDisconnected  # pylint: disable=import-error,import-outside-toplevel
        classes.append(SMPTransportDisconnected)
    except ImportError:
        pass
    try:
        from smpclient.exceptions import SMPClientException  # pylint: disable=import-error,import-outside-toplevel
        classes.append(SMPClientException)
    except ImportError:
        pass
    return tuple(classes)



#: over UDP, a request is sent again when no reply has come in this long (a datagram lost on
#: the way there or back); the client's request timeout still bounds the whole wait
UDP_RESEND_S = 2.0


def _sequence(frame):
    """An SMP frame's sequence number: its header's seventh byte."""
    return frame[6]


def _upload_start(frame):
    """The first request of an image upload (image group, upload, a body carrying `len`):
    the device erases the spare slot before it answers, and the same request again would
    start the erase again, so it is never sent twice."""
    return len(frame) > 8 and frame[4:6] == b"\x00\x01" and frame[7] == 1 and b"clen" in frame[8:]


async def _reply_to(send, receive, request, resend_s=None):
    """Send `request` and return the reply that carries its sequence number. Over UDP a
    datagram can be lost, delivered twice or late: with `resend_s`, the same request (same
    sequence) goes again each time that long passes with no reply, which is safe for every
    request but an upload's first (see `_upload_start`); a reply to an earlier request, or a
    second reply to this one after it was answered, is dropped and the wait goes on. The
    client's request timeout bounds it all."""
    await send(request)
    while True:
        try:
            if resend_s:
                frame = await asyncio.wait_for(receive(), resend_s)
            else:
                frame = await receive()
        except asyncio.TimeoutError:
            await send(request)
            continue
        if len(frame) < 8 or len(request) < 8 or _sequence(frame) == _sequence(request):
            return frame


def _udp_transport(port):
    """smpclient's UDP transport, connecting to `port` (its connect takes the port, the
    client does not pass one), sending a request again when its reply is lost and waiting
    past a stale one (`_reply_to`)."""
    from smpclient.transport.udp import SMPUDPTransport  # pylint: disable=import-outside-toplevel,import-error

    class AtPort(SMPUDPTransport):
        async def connect(self, address, timeout_s, port=None):  # pylint: disable=arguments-differ,unused-argument
            return await super().connect(address, timeout_s, port=_port)

        async def send_and_receive(self, data):
            return await _reply_to(self.send, self.receive, data,
                                   None if _upload_start(data) else UDP_RESEND_S)

    _port = port
    return AtPort()


class SmpFlasher(common.Flasher):
    # a plan may take the board's last full read (docs/ARCHITECTURE.md#checking-depth)
    spot_reads = ("identity",)

    def __init__(self, name, manifest, image_dir, transport="serial", address=None, board=None,
                 board_tag=None, declared=None, devreg="~/.devreg/devreg.py",
                 settle=240.0, devlock_dir="~/.devreg", adapter=BLE_ADAPTER,
                 ble_lock_wait=BLE_LOCK_WAIT, rebond="auto", console=None,
                 port_from="devreg", run_dir=None, bearer=None, udp_port=UDP_PORT,
                 delivery=None, delivery_timeout=1800.0):
        self.name, self.manifest = name, manifest
        # A route that carries the image to the device itself (flash/delivery.py): the upload,
        # the test mark and the reset are then the device's own client's, and so is the
        # confirmation. The device is read over MCUmgr before and after all the same.
        self.delivery, self.delivery_timeout, self.delivered = delivery, delivery_timeout, None
        # A simulated board (run_dir): its serial port is whatever pty its broker releases
        # (port_from "broker"), and it runs an executable, not a slot: it is known by the
        # executable's bytes, mapped to the image whose exe_hash they are.
        self.port_from, self.run_dir, self.released_port = port_from, run_dir, None
        self.pid_before = None
        self.image = os.path.join(image_dir, IMAGE_FILE)
        self.transport, self.address, self.b = transport, address, board
        # udp: the registry route it goes over (eth or wifi), what devreg guards its address by
        self.bearer = bearer or ("ble" if transport == "ble" else None)
        self.udp_port = int(udp_port or UDP_PORT)
        self.board_tag, self.declared = board_tag, declared
        self.devreg = os.path.expanduser(devreg)
        self.devlock_dir = os.path.expanduser(devlock_dir)
        self.adapter, self.ble_lock_wait, self.rebond = adapter, ble_lock_wait, rebond
        self.settle = settle
        self.console = console  # BLE: the board's broker, when it also has a console here
        self.flags = None
        self.held = False
        self.route_checked = False
        self.rebonded = None    # set to the reason when a stale bond was replaced

    # ---- one MCUmgr session -------------------------------------------------------
    def _address(self):
        if self.transport in ("ble", "udp"):
            # Once per flasher, before the first session: the address is the registry's
            # for this board or nothing is connected to.
            if not self.route_checked:
                why = common.route_refusal(self.devreg, self.name, self.bearer, self.address)
                if why:
                    raise common.FlashError(why)
                self.route_checked = True
            return self.address
        if self.port_from == "broker":
            if not self.released_port:
                raise common.FlashError(f"{self.name}: its broker has not released a port to speak MCUmgr on")
            return self.released_port
        port, why = common.guarded_port(self.devreg, self.name)
        if why:
            raise common.FlashError(why)
        return port

    def _client(self):
        from smpclient import SMPClient  # pylint: disable=import-outside-toplevel
        if self.transport == "ble":
            from smpclient.transport.ble import SMPBLETransport  # pylint: disable=import-outside-toplevel
            transport = SMPBLETransport()
        elif self.transport == "udp":
            transport = _udp_transport(self.udp_port)
        else:
            from smpclient.transport.serial import (  # pylint: disable=import-outside-toplevel
                BufferParams, SMPSerialTransport)
            # The library's default frame is more than a shell transport's line buffers
            # take (10 lines of 127), and an upload chunk that long is never answered.
            transport = SMPSerialTransport(BufferParams(line_length=128, line_buffers=10))
        return SMPClient(transport, self._address(), timeout_s=10.0)

    def _session(self, work, timeout=60.0):
        """Run `await work(client)` in one connection. For serial, the board's broker gives
        up the console first and takes it back after, unless a flash is holding it."""
        async def go():
            for attempt in range(BLE_CONNECT_TRIES if self.transport == "ble" else 1):
                client = self._client()
                try:
                    await asyncio.wait_for(client.connect(connect_timeout_s=BLE_CONNECT_S),
                                           BLE_CONNECT_S + 10)
                    break
                except Exception as e:  # pylint: disable=broad-except
                    # bleak's cold "failed to discover services" clears on a second
                    # connection; anything else fails the same way twice.
                    if attempt + 1 >= BLE_CONNECT_TRIES or self.transport != "ble":
                        raise ConnectError(f"MCUmgr over {self.transport}: cannot "
                                           f"connect: {type(e).__name__} {e}") from e
                    await asyncio.sleep(2)
            try:
                return await asyncio.wait_for(work(client), timeout)
            finally:
                try:
                    await asyncio.wait_for(client.disconnect(), 10)
                except Exception:  # pylint: disable=broad-except
                    pass
        lock = self._adapter_lock() if self.transport == "ble" else None
        if lock is not None:
            self._free_ble_slot()
        borrowed = self.transport == "serial" and self.b is not None and not self.held
        if borrowed:
            self.released_port = self.b.release(ttl=timeout + 60)
        try:
            return asyncio.run(go())
        except (asyncio.TimeoutError, OSError) as e:
            raise common.FlashError(f"MCUmgr over {self.transport}: {type(e).__name__} {e}") from e
        except _smp_session_errors() as e:
            # smpclient's own exceptions (a link that dropped mid-session, a bad sequence
            # number) are plain Exceptions: named here so the verify loop retries the session
            # instead of the module falling over after the write already went through.
            raise common.FlashError(f"MCUmgr over {self.transport}: {type(e).__name__} {e}") from e
        finally:
            if borrowed:
                self.b.reacquire()
            if lock is not None:
                lock.release()

    def _adapter_lock(self):
        """Hold the host's Bluetooth adapter (devlock resource `ble:<adapter>`) for one session.
        Waits for a busy adapter; refuses naming the holder when the wait runs out."""
        if self.devlock_dir not in sys.path:
            sys.path.insert(0, self.devlock_dir)
        try:
            from devlock import DevLock  # pylint: disable=import-error,import-outside-toplevel
        except ImportError as e:
            raise common.FlashError(f"no devlock in {self.devlock_dir} (deploy the host: "
                                    f"`just hosts-setup <layout>`): {e}") from e
        lock = DevLock(f"ble:{self.adapter}", tool="rxrbench-smp_flash", device=self.name,
                       purpose=f"MCUmgr session with {self.name}")
        if not lock.acquire(wait_s=self.ble_lock_wait):
            held = lock.holder_summary()
            lock.release()
            raise common.FlashError(f"the Bluetooth adapter {self.adapter} is busy: {held} "
                                    f"(waited {self.ble_lock_wait:.0f} s)")
        return lock

    def _free_ble_slot(self):
        """Make sure the host holds no link to the device before a session.

        A node advertises only while it has a free connection slot, so a link the host
        already holds can make the scan time out (docs/HARDWARE.md).
        """
        _, out = common.tool(["bluetoothctl", "--timeout", "10", "info", self.address], 20)
        if "Connected: yes" in out:
            common.tool(["bluetoothctl", "--timeout", "15", "disconnect", self.address], 25)
            time.sleep(2)   # the node re-arms advertising on its disconnect

    @staticmethod
    async def _states(client):
        from smpclient.requests.image_management import ImageStatesRead  # pylint: disable=import-outside-toplevel
        r = await client.request(ImageStatesRead())
        if not hasattr(r, "images"):
            raise common.FlashError(f"the device refused the image-state read: {r}")
        return [{"slot": i.slot, "version": i.version, "hash": i.hash.hex() if i.hash else None,
                 "active": bool(i.active), "confirmed": bool(i.confirmed),
                 "pending": bool(i.pending)} for i in r.images]

    def _bluez_paired(self):
        """Whether this host's BlueZ holds a bond for the device (`Paired: yes`; newer
        bluetoothctl says `Bonded: yes` as well)."""
        _, out = common.tool(["bluetoothctl", "--timeout", "10", "info", self.address], 20)
        return "Paired: yes" in out or "Bonded: yes" in out

    def _remove_bond(self):
        common.tool(["bluetoothctl", "--timeout", "15", "remove", self.address], 25)
        time.sleep(2)   # bluetoothd drops the device object; the next scan recreates it

    def _host_address(self):
        """This host's own Bluetooth address, from BlueZ (`Controller <address> (public)`);
        None when it cannot be read."""
        _, out = common.tool(["bluetoothctl", "--timeout", "10", "show"], 20)
        m = re.search(r"Controller\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", out or "")
        return m.group(1) if m else None

    def _board_bond(self):
        """The name of the bond the board holds for this host (its console kind's
        `bonds_prefix` and the host's address), "" when it
        holds none, None when it cannot be asked: no console, a family whose console has
        no such command, or no answer."""
        spec = familyspec.of(self.manifest["family"])
        cmd, prefix = spec["bonds_command"], spec["bonds_prefix"] or ""
        host = self._host_address() if self.console is not None and cmd else None
        if not host:
            return None
        try:
            out = self.console.exec(cmd, deadline=8.0)
        except (broker_client.BrokerError, stateline.StateError, OSError):
            return None
        want = (prefix + host.replace(":", "")).lower()
        for line in out.splitlines():
            if line.strip().lower().startswith(want):
                return line.strip()
        return ""

    # ---- the four steps -------------------------------------------------------------
    def _transport_refusal(self):
        """An image staged from its build says which MCUmgr transports it has; the board runs it
        (or its sibling of the same recipe) and would never answer on another."""
        have = self.manifest.get("mcumgr")
        if have is not None and self.transport not in have:
            return [f"the image has no MCUmgr over {self.transport} (it has "
                    f"{', '.join(have) or 'none'}): an update that way could never be answered; "
                    "use another route (routes: {flash: ...}) or a build that has it"]
        return []

    def device(self):
        # said before the board is asked anything: the read itself would wait for nothing
        why = self._transport_refusal()
        if why:
            raise common.FlashError("refused: " + "; ".join(why))
        try:
            images = self._session(self._states)
        except common.FlashError as e:
            if self.transport != "ble" or self.rebond == "never":
                raise
            if isinstance(e, ConnectError):
                # No exchange: a dead link or a stale bond. The bond goes only when the host
                # holds one and the board, asked, holds none for it; `force` changes nothing
                # here: a scan timeout says nothing about the keys.
                if self.console is None or not self._bluez_paired() or self._board_bond() != "":
                    raise
                paired, held = True, ""
            else:
                paired = self._bluez_paired()
                held = None if self.rebond == "force" and paired else self._board_bond()
            if not paired:
                if held:
                    raise common.FlashError(
                        f"{e} (the board holds a bond for this host, {held}, that this host "
                        f"no longer has, and it refuses a new pairing over it: delete the "
                        f"board's record of this host on its console and reboot it)") from e
                raise
            if self.rebond != "force" and held != "":
                # Connected, then no answer, on a bond this host holds. A stale bond and a
                # link that dropped look the same from here: the board says which it is.
                if held:
                    raise common.FlashError(
                        f"{e} (this host's bond for {self.address} is on the board too, as "
                        f"{held}: the link dropped, the keys are good, nothing was removed)") from e
                if held is None:
                    raise common.FlashError(
                        f"{e} (this host holds a bond for {self.address} and the board could "
                        f"not be asked whether it still does, so nothing was removed; "
                        f"`rebond: force` removes it without asking)") from e
            # The device no longer has the bond this host holds (a DFU wiped it): remove
            # the host's and let BlueZ pair afresh, once.
            self._remove_bond()
            try:
                images = self._session(self._states)
            except common.FlashError as e2:
                raise common.FlashError(
                    f"{e2} (after removing this host's bond for {self.address}, which had "
                    f"failed with: {e})") from e2
            self.rebonded = (f"this host's bond for {self.address} was stale ({e}); "
                             f"removed, and a fresh encrypted session answered")
        if self.run_dir and not any(i.get("active") for i in images):
            return self._sim_identity()
        ident, self.flags = identity.of_smp(images, family=self.manifest["family"])
        return ident

    def _reborrow(self, end):
        """After the reset, the board's broker takes the console back (it finds the board
        wherever it came up; for a simulated board, it is what starts the board again) and
        gives it up again for the sessions that follow."""
        why = "the board did not come back to its broker"
        if self.run_dir and not self.manifest.get("qemu"):
            # Not before the reset has happened (the request is answered before the board
            # goes down): a simulated board's process has exited by then, and it is its
            # broker's open that starts it again (the sim's opener does), so the broker is
            # asked only once the old process is gone. (An emulated MCU resets inside its
            # machine: the process and its pty stay.)
            while time.monotonic() < end and not self._sim_gone(self.pid_before):
                time.sleep(0.5)
        while time.monotonic() < end:
            try:
                r = self.b.reacquire()
                if r.get("ok"):
                    self.released_port = self.b.release(ttl=self.settle + 900)
                    return
                why = r.get("error") or why
            except broker_client.BrokerError as e:
                why = str(e)
            time.sleep(1)
        raise common.FlashError(f"after the reset: {why}")

    @staticmethod
    def _sim_gone(pid):
        """The process that was the board before the reset is gone, or a zombie nobody has
        collected yet."""
        if pid is None:
            return True
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return True

    def _sim_pid(self):
        try:
            with open(os.path.join(self.run_dir, f"{self.name}.pid")) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def _sim_identity(self):
        """A simulated board runs an executable, not a slot (no bootloader puts one there),
        so it is known by that executable's bytes: the image whose exe_hash they are, by the
        image's own hash; any other bytes by themselves."""
        pid = self._sim_pid()
        if pid is None:
            raise common.Unanswered(f"{self.name}: no running sim board in {self.run_dir}")
        try:
            exe_hash = common.sha256_of(f"/proc/{pid}/exe")
        except OSError as e:
            raise common.Unanswered(f"{self.name}: no running sim board in {self.run_dir} ({e})") from e
        self.flags = {"slot": 0, "confirmed": True, "pending": False}
        known = exe_hash == self.manifest.get("exe_hash")
        return identity.make(self.manifest["family"], hash=self.manifest["hash"] if known else exe_hash,
                             version=self.manifest.get("version") if known else None)

    def preflight(self):
        out = common.common_refusals(self.manifest, ("esp-parts", "uf2", "mcuboot-bin"),
                                     self.board_tag, self.declared)
        if not os.path.isfile(self.image):
            out.append(f"no {IMAGE_FILE} beside the manifest: MCUmgr uploads the signed image")
        if not self.manifest.get("hash"):
            out.append("the manifest has no hash: the upload could not be verified")
        if self.transport in ("ble", "udp") and not self.address:
            out.append(f"the {self.bearer or self.transport} route has no address")
        if self.transport == "udp" and self.bearer not in UDP_BEARERS:
            out.append(f"MCUmgr over udp goes over a network route ({', '.join(UDP_BEARERS)}), "
                       f"not {self.bearer or 'none'}")
        out += self._transport_refusal()
        if self.flags is not None and not self.flags["confirmed"]:
            out.append("the running image is not confirmed: the device is still on trial "
                       "with its last update")
        if self.delivery is not None and not out:
            out += self.delivery.blockers({"format": common.DELIVERS["smp"]})
        try:
            import smpclient  # noqa: F401  pylint: disable=import-outside-toplevel,unused-import
        except ImportError:
            out.append("no `smpclient` library for this module's Python")
        return out

    def _deliver(self):
        """The write, by a route that carries the image itself: say which image, then wait
        until the device runs it, which is after its own client has fetched it, reset, and
        MCUboot has swapped it in. The device asks on its own schedule and does not answer
        MCUmgr while it swaps, so a look that fails is only a look that failed.

        A delivery the route closes as failed before the device runs the image failed on the
        device (a download, a hash, a full slot): the route's own word says why. One it closes
        as done while the device runs something else is left to `verify`, which reads the
        device and says what it runs."""
        self.delivered = self.delivery.deliver(
            self.image, {**self.manifest, "format": common.DELIVERS["smp"]},
            name=os.path.basename(os.path.dirname(os.path.abspath(self.image))))
        end = time.monotonic() + self.delivery_timeout
        while True:
            try:
                images = self._session(self._states)
                if any(i["active"] and i["hash"] == self.manifest["hash"] for i in images):
                    return
            except common.FlashError:
                pass
            state = self.delivery.state()
            if not state.get("open"):
                if state.get("result") == "failure":
                    raise common.FlashError(
                        f"the {self.delivery.bearer} delivery failed before the device ran the "
                        f"image: {state.get('detail') or 'no detail'}")
                return
            if time.monotonic() > end:
                raise common.FlashError(
                    f"the device did not take the {self.delivery.bearer} delivery and run the image "
                    f"within {self.delivery_timeout:.0f} s "
                    f"({state.get('detail') or 'the route has no word of it'})")
            time.sleep(5)

    def write(self):
        if self.delivery is not None:
            self._deliver()
            return
        from smpclient.requests.image_management import ImageStatesWrite  # pylint: disable=import-outside-toplevel
        from smpclient.requests.os_management import ResetWrite  # pylint: disable=import-outside-toplevel
        with open(self.image, "rb") as f:
            data = f.read()
        want = bytes.fromhex(self.manifest["hash"])

        async def upload(client):
            async for _offset in client.upload(data, first_timeout_s=60.0):
                pass
            images = await self._states(client)
            if not any(i["hash"] == self.manifest["hash"] and not i["active"] for i in images):
                raise common.FlashError("after the upload the spare slot does not hold the image")
            await client.request(ImageStatesWrite(hash=want, confirm=False))   # a TEST boot
            await client.request(ResetWrite())

        if self.transport == "serial" and self.b is not None:
            self.released_port = self.b.release(ttl=self.settle + 900)
            self.held = True
        self.pid_before = self._sim_pid() if self.run_dir else None
        try:
            self._session(upload, timeout=900.0)
        except Exception:
            # The write failed: nothing was marked for boot, the device runs what it ran.
            # The console goes back to its broker now, not when the release times out.
            if self.held:
                self.held = False
                self.b.reacquire()
            raise

    def verify(self):
        from smpclient.requests.image_management import ImageStatesWrite  # pylint: disable=import-outside-toplevel
        want = bytes.fromhex(self.manifest["hash"])

        async def confirm(client):
            images = await self._states(client)
            active = next((i for i in images if i["active"]), None)
            # A delivered image is confirmed by the device's own client, when it judges
            # itself healthy: that is waited for here, never done for it.
            if (self.delivery is None and active and active["hash"] == self.manifest["hash"]
                    and not active["confirmed"]):
                await client.request(ImageStatesWrite(hash=want, confirm=True))
                images = await self._states(client)
            return images

        end = time.monotonic() + self.settle
        why = "the device did not answer MCUmgr after the reset"
        if self.port_from == "broker":
            self._reborrow(end)
        try:
            while time.monotonic() < end:
                time.sleep(5)       # the swap copies a whole slot before anything answers
                try:
                    images = self._session(confirm)
                except common.FlashError as e:
                    why = str(e)
                    continue
                if self.run_dir and not any(i.get("active") for i in images):
                    spare = next((i for i in images if i["hash"] == self.manifest["hash"]), None)
                    raise common.FlashError(
                        "the spare slot holds the image" + (", marked for test" if spare and spare["pending"] else "")
                        + ", but nothing swapped it in: a simulated board has no bootloader "
                          "(the swap is MCUboot's, on a board that boots from flash)")
                ident, self.flags = identity.of_smp(images, family=self.manifest["family"])
                if ident["hash"] != self.manifest["hash"]:
                    spare = next((i for i in images if i["hash"] == self.manifest["hash"]), None)
                    if spare and _newer(ident.get("version"), self.manifest.get("version")):
                        # MCUboot in direct-XIP or RAM-load mode runs the highest version it
                        # holds; the candidate is in the spare slot, where it stays.
                        raise common.FlashError(
                            f"the device runs its previous image, version {ident.get('version')}: the "
                            f"bootloader runs the highest version it holds and never a lower one "
                            f"(direct-XIP or RAM-load mode), so {self.manifest.get('version')} stays in "
                            "the spare slot; a downgrade on such a board is a power cycle to its "
                            "factory image, or an upload of a higher version")
                    raise common.FlashError(
                        "the device runs its previous image: the new one did not boot, or "
                        "MCUboot swapped back")
                if not self.flags["confirmed"] and self.delivery is not None:
                    why = ("the new image runs but the device has not confirmed it: "
                           "its next reset will swap back")
                    continue
                if not self.flags["confirmed"]:
                    raise common.FlashError("the new image runs but could not be confirmed: "
                                            "the next reset will swap back")
                if self.transport == "ble" and self.console is not None:
                    # MCUmgr answered; the console comes back with its broker a little
                    # later, and the next thing to read the board would find it closed.
                    try:
                        common.wait_back(self.console, self.manifest["family"], self.settle)
                    except common.FlashError as e:
                        raise common.FlashError(
                            f"the new image runs and is confirmed, but the board's console "
                            f"did not come back: {e}") from e
                return ident
            raise common.FlashError(why)
        finally:
            if self.held:
                self.held = False
                self.b.reacquire()
