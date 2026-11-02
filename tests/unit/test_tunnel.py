import base64

import pytest
from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

from ansible_collections.rxrbench.mesh.plugins.module_utils import tunnel
from tests.support.fake_api import FakeApi, crc16

HEARTBEAT = bytes.fromhex("3a00")           # ToRadio{heartbeat{}}
NODE = 0x12345678


def frame_line(data):
    text = base64.b64encode(data).decode()
    return f"~P{text}*{crc16(text.encode()):04x}"


# ---- the text framing ---------------------------------------------------------------------

def test_a_short_frame_is_one_command_with_its_checksum():
    assert tunnel.commands(HEARTBEAT) == [f"meshtastic api OgA= {crc16(HEARTBEAT):04x}"]


def test_a_long_frame_is_cut_into_parts_that_fit_a_shell_line():
    data = bytes(range(256)) + bytes(range(200))
    cmds = tunnel.commands(data)
    assert len(cmds) == 4
    assert all(c.startswith("meshtastic api + ") for c in cmds[:-1])
    assert all(len(c) < 250 for c in cmds), "the shell's line is 256 characters"
    # every part is base64 by itself, and together they are the frame
    parts = [c.split()[3] for c in cmds[:-1]] + [cmds[-1].split()[2]]
    assert b"".join(base64.b64decode(p, validate=True) for p in parts) == data
    assert cmds[-1].split()[3] == f"{crc16(data):04x}"


def test_what_cannot_be_a_frame_is_not_sent():
    with pytest.raises(tunnel.TunnelError):
        tunnel.commands(b"")
    with pytest.raises(tunnel.TunnelError):
        tunnel.commands(bytes(513))


def test_parse_reads_frames_and_status_through_console_noise():
    out = ("\x1b[1;32muart:~$ \x1b[mmeshtastic api\r\n"
           "[00:01:02.003,000] <inf> meshtastic: RX from 0x0a0b0c0d\r\n"
           + frame_line(b"\x01\x02\x03") + "\r\n" + frame_line(b"\xff" * 300) + "\r\n~P+2\r\n")
    reply = tunnel.parse(out)
    assert reply.frames == [b"\x01\x02\x03", b"\xff" * 300]
    assert reply.more and reply.n == 2
    assert not tunnel.parse("~P.0\n").more


@pytest.mark.parametrize("out, why", [
    ("", "no status line"),
    ("meshtastic: command not found\n", "no status line"),
    (frame_line(b"abc") + "\n", "no status line"),                       # cut short
    (frame_line(b"abc")[:-1] + "0\n~P.1\n", "checksum"),                 # a damaged frame
    (frame_line(b"abc").replace("*", "") + "\n~P.1\n", "damaged line"),
    (frame_line(b"abc") + "\n~P.2\n", "served 2 frames, 1 arrived"),     # a whole line lost
    ("~P.0\n" + frame_line(b"abc") + "\n", "after the status line"),
])
def test_parse_refuses_an_answer_that_is_not_whole(out, why):
    with pytest.raises(tunnel.TunnelError, match=why):
        tunnel.parse(out)


def test_parse_raises_the_node_s_refusal_by_name():
    with pytest.raises(tunnel.Refused) as e:
        tunnel.parse("~P!len\n")
    assert e.value.why == "len"


# ---- one request ----------------------------------------------------------------------------

def test_a_send_returns_the_frames_it_drew_and_polls_find_the_rest():
    fake = FakeApi(handle=lambda toradio: [b"f%d" % i for i in range(6)], burst=4)
    t = tunnel.Tunnel(fake)
    reply = t.send(HEARTBEAT)
    assert reply.frames == [b"f0", b"f1", b"f2", b"f3"] and reply.more
    assert t.drain() == [b"f4", b"f5"]
    assert fake.received == [HEARTBEAT]


def test_a_long_frame_arrives_whole():
    fake = FakeApi()
    data = bytes(range(256)) + bytes(range(150))
    tunnel.Tunnel(fake).send(data)
    assert fake.received == [data]
    assert len(fake.commands) == 3


def test_a_byte_lost_on_the_way_in_is_sent_again():
    fake = FakeApi()
    fake.mangle_in = lambda cmd: cmd[:-9] + cmd[-8:]      # one base64 character gone
    tunnel.Tunnel(fake).send(bytes(range(40)))
    assert fake.received == [bytes(range(40))], "exactly once, and undamaged"


def test_a_busy_node_is_asked_again_and_a_final_refusal_is_raised():
    fake = FakeApi()
    fake.busy = 2
    tunnel.Tunnel(fake).send(HEARTBEAT)
    assert fake.received == [HEARTBEAT]
    fake.busy = 5
    with pytest.raises(tunnel.Refused):
        tunnel.Tunnel(fake).send(HEARTBEAT)


def test_a_refusal_that_a_retry_cannot_cure_is_not_retried():
    fake = FakeApi()
    t = tunnel.Tunnel(fake)
    t.run = lambda cmd: "~P!len"
    with pytest.raises(tunnel.Refused):
        t.send(HEARTBEAT)


def test_a_damaged_answer_is_an_error_not_a_short_read():
    fake = FakeApi(handle=lambda toradio: [b"abc", b"def"])
    fake.mangle_out = lambda out: out.replace("\n", "\n~Pgarbage\n", 1)
    with pytest.raises(tunnel.TunnelError):
        tunnel.Tunnel(fake).send(HEARTBEAT)


def test_firmware_without_the_command_says_so():
    with pytest.raises(tunnel.TunnelError, match="no `meshtastic api`"):
        tunnel.Tunnel(lambda cmd: "api: command not found").poll()


# ---- the stock library on top -----------------------------------------------------------------

class FakeNode:
    """Enough of a node for the stock library to connect, configure and send."""

    def __init__(self):
        self.owner = "before"
        self.texts = []
        self.disconnects = 0

    def handle(self, raw):
        to = mesh_pb2.ToRadio()
        to.ParseFromString(raw)
        which = to.WhichOneof("payload_variant")
        out = []
        if which == "want_config_id":
            f = mesh_pb2.FromRadio()
            f.my_info.my_node_num = NODE
            out.append(f)
            f = mesh_pb2.FromRadio()
            f.node_info.num = NODE
            f.node_info.user.id = f"!{NODE:08x}"
            f.node_info.user.long_name = self.owner
            out.append(f)
            f = mesh_pb2.FromRadio()
            f.channel.index = 0
            f.channel.role = 1
            f.channel.settings.psk = b"\x01"
            out.append(f)
            f = mesh_pb2.FromRadio()
            f.config.lora.modem_preset = 6
            f.config.lora.tx_power = 2
            out.append(f)
            f = mesh_pb2.FromRadio()
            f.config_complete_id = to.want_config_id
            out.append(f)
        elif which == "packet":
            if to.packet.decoded.portnum == portnums_pb2.ADMIN_APP:
                a = admin_pb2.AdminMessage()
                a.ParseFromString(to.packet.decoded.payload)
                if a.HasField("set_owner"):
                    self.owner = a.set_owner.long_name
            elif to.packet.decoded.portnum == portnums_pb2.TEXT_MESSAGE_APP:
                self.texts.append(to.packet.decoded.payload.decode())
            f = mesh_pb2.FromRadio()
            f.queueStatus.free = 16
            f.queueStatus.maxlen = 16
            f.queueStatus.mesh_packet_id = to.packet.id
            out.append(f)
        elif which == "disconnect":
            self.disconnects += 1
        return [f.SerializeToString() for f in out]


@pytest.fixture
def node():
    n = FakeNode()
    n.api = FakeApi(handle=n.handle, burst=2)
    return n


def test_the_stock_library_connects_and_reads_the_node(node):
    iface = tunnel.TunnelInterface(node.api, poll_interval=0.02, connect_timeout=10)
    try:
        assert iface.myInfo.my_node_num == NODE
        assert iface.getLongName() == "before"
        assert iface.localNode.localConfig.lora.tx_power == 2
        assert iface.localNode.channels[0].settings.psk == b"\x01"
    finally:
        iface.close()
    assert node.disconnects == 1, "close tells the node the session is over"


def test_the_stock_library_writes_and_sends_through_it(node):
    iface = tunnel.TunnelInterface(node.api, poll_interval=0.02, connect_timeout=10)
    try:
        iface.localNode.setOwner("a name long enough to need more than one line " * 3)
        iface.sendText("hello")
    finally:
        iface.close()
    assert node.owner.startswith("a name long enough")
    assert node.texts == ["hello"]
    assert any(c.startswith("meshtastic api + ") for c in node.api.commands), \
        "the long admin packet went in parts"


def test_traffic_that_arrives_by_itself_reaches_the_library(node):
    from pubsub import pub
    got = []

    def on_text(packet, interface):  # pylint: disable=unused-argument
        got.append(packet["decoded"]["text"])

    pub.subscribe(on_text, "meshtastic.receive.text")
    iface = tunnel.TunnelInterface(node.api, poll_interval=0.02, connect_timeout=10)
    try:
        f = mesh_pb2.FromRadio()
        setattr(f.packet, "from", 0x0a0b0c0d)
        f.packet.to = 0xFFFFFFFF
        f.packet.decoded.portnum = portnums_pb2.TEXT_MESSAGE_APP
        f.packet.decoded.payload = b"heard on the mesh"
        node.api.push(f.SerializeToString())
        import time
        end = time.time() + 5
        while not got and time.time() < end:
            time.sleep(0.02)
    finally:
        iface.close()
        pub.unsubscribe(on_text, "meshtastic.receive.text")
    assert got == ["heard on the mesh"]


def test_a_broken_console_fails_the_connect_instead_of_hanging():
    def dead(cmd):
        raise OSError("the broker went away")
    with pytest.raises(Exception, match="broker went away"):
        tunnel.TunnelInterface(dead, poll_interval=0.02, connect_timeout=5)


def test_a_damaged_line_in_a_config_poll_asks_for_the_config_again(node):
    # One damaged line in a poll's answer loses that poll's frames; before the node's config
    # has arrived whole, the session asks for it afresh instead of failing the plan (a damaged
    # line turns up about once in twenty polls on a real board).
    calls = [0]

    def run(cmd):
        out = node.api(cmd)
        calls[0] += 1
        return out.replace("\n", "\n~Pgarbage\n", 1) if calls[0] == 2 else out
    iface = tunnel.TunnelInterface(run, poll_interval=0.02, connect_timeout=10)
    try:
        assert iface.localNode.localConfig.lora.tx_power == 2
        assert iface.getLongName() == "before"
    finally:
        iface.close()
    assert calls[0] >= 3, "the config was asked for again"
