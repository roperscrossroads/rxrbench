"""Scenario steps on a stock Meshtastic node, through its own phone API: the node has no
console, so a step is one of a few commands this module speaks for it.

    text <node> <message>   a direct text that asks for an acknowledgement, and waits for it:
                            `ack from 0x<node>`, `nak from 0x<node>: <reason>`, `implicit ack`
                            (its own packet heard relayed, not the receiver's answer) or
                            `no answer`
    nodes                   the node database, a line per node, newest first, in the shape a
                            zephyrtastic console's `meshtastic nodedb list` prints
                            (`0x<node> last=<n>s ago snr=<db> hops=<n> long="..." short="..."`)
    info                    the node itself: `node: 0x<node>`, its owner and firmware
    announce                its own NodeInfo, broadcast now (its public key with it): a node
                            that lost the key (a fresh flash) can text it again

A node is named `0x1234abcd`, `!1234abcd` or by its number. A node stamps what it hears with
its own clock (`lastHeard`), which it takes from the mesh or a phone and which may be far off
(an nRF52 has no clock that keeps time over a reset). So an age is counted on the node's clock:
from its own entry's stamp, which it keeps near its `now`, else from the newest stamp it holds;
the first line says how far that is from this host's clock.
"""
import shlex
import threading
import time

TEXT_PORT = 1           # TEXT_MESSAGE_APP
NODEINFO_PORT = 4       # NODEINFO_APP


class StepError(ValueError):
    pass


def node_num(name):
    s = str(name).strip()
    try:
        if s.startswith("!"):
            return int(s[1:], 16)
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s)
    except ValueError as e:
        raise StepError(f"{name!r} is not a node (0x1234abcd, !1234abcd or a number)") from e


def parse(cmd):
    """(verb, args) of a step's command, or StepError."""
    try:
        words = shlex.split(cmd)
    except ValueError as e:
        raise StepError(f"{cmd!r}: {e}") from e
    if not words:
        raise StepError("an empty command")
    verb, rest = words[0], words[1:]
    if verb == "text":
        if len(rest) < 2:
            raise StepError("usage: text <node> <message>")
        return "text", (node_num(rest[0]), " ".join(rest[1:]))
    if verb in ("nodes", "info", "announce"):
        if rest:
            raise StepError(f"usage: {verb}")
        return verb, ()
    raise StepError(f"{verb!r}: a stock node's steps are text, nodes, info and announce")


def nodes_text(nodes, host_now, me=None):
    """The node database as lines, newest first (never heard last), ages on the node's clock."""
    stamps = [n.get("lastHeard") for n in (nodes or {}).values() if n.get("lastHeard")]
    own = next((n.get("lastHeard") for n in (nodes or {}).values()
                if me is not None and n.get("num") == me and n.get("lastHeard")), None)
    now = own or (max(stamps) if stamps else host_now)
    rows = []
    for n in (nodes or {}).values():
        num = n.get("num")
        if num is None:
            continue
        user = n.get("user") or {}
        heard = n.get("lastHeard")
        rows.append((-(heard or 0), num,
                     f"0x{num:08x} last=" + (f"{max(0, int(now - heard))}s ago" if heard
                                             else "never")
                     + f" snr={float(n.get('snr') or 0.0):.1f}"
                     + f" hops={n.get('hopsAway', '?')}"
                     + f" long=\"{user.get('longName', '')}\" short=\"{user.get('shortName', '')}\""))
    rows.sort()
    head = f"nodes: {len(rows)}"
    if own:
        head += f" (node clock {int(now - host_now):+d} s from this host's)"
    elif stamps:
        # A node with no valid time stamps nothing it hears: its stamps are as old as its time
        # (its own entry has none), so these ages say nothing about now.
        head += (" (node clock unknown: its own entry has no stamp; ages are from its newest, "
                 f"{int(now - host_now):+d} s from this host's)")
    return "\n".join([head] + [r[2] for r in rows])


def info_text(iface):
    me = iface.getMyNodeInfo() or {}
    user = me.get("user") or {}
    num = me.get("num") or getattr(getattr(iface, "myInfo", None), "my_node_num", None)
    fw = getattr(getattr(iface, "metadata", None), "firmware_version", None)
    return "\n".join([f"node: 0x{num:08x}" if num is not None else "node: ?",
                      f"owner: long=\"{user.get('longName', '')}\" short=\"{user.get('shortName', '')}\"",
                      f"firmware: {fw or '?'}"])


def send_text(iface, num, message, timeout, me=None, clock=time.monotonic):
    """A direct text with want_ack, and what came back for it within `timeout` seconds."""
    done = threading.Event()
    said = {}

    def answered(packet):
        said.update(packet)
        done.set()

    iface.sendData(message.encode(), destinationId=num, portNum=TEXT_PORT, wantAck=True,
                   onResponse=answered, onResponseAckPermitted=True)
    start = clock()
    done.wait(timeout)
    if not said:
        return f"no answer within {int(timeout)} s"
    frm = said.get("from")
    who = f"0x{frm:08x}" if isinstance(frm, int) else str(frm)
    reason = ((said.get("decoded") or {}).get("routing") or {}).get("errorReason", "NONE")
    took = f" after {clock() - start:.1f} s"
    if reason in (None, "NONE"):
        if me is not None and frm == me:
            return f"implicit ack (heard relayed, no answer from 0x{num:08x}){took}"
        return f"ack from {who}{took}"
    return f"nak from {who}: {reason}{took}"


def announce(iface, user_bytes=None):
    """Broadcast the node's own User record on the NodeInfo port, as it does at boot."""
    if user_bytes is None:
        from google.protobuf import json_format  # pylint: disable=import-outside-toplevel
        from meshtastic.protobuf import mesh_pb2  # pylint: disable=import-outside-toplevel
        user = (iface.getMyNodeInfo() or {}).get("user") or {}
        user_bytes = json_format.ParseDict(user, mesh_pb2.User(),
                                           ignore_unknown_fields=True).SerializeToString()
    iface.sendData(user_bytes, destinationId="^all", portNum=NODEINFO_PORT)
    me = (iface.getMyNodeInfo() or {}).get("num")
    return f"announced 0x{me:08x}" if me is not None else "announced"


def run(iface, cmd, timeout, clock=time.time):
    """The output of one step on a connected interface."""
    verb, args = parse(cmd)
    if verb == "text":
        me = getattr(getattr(iface, "myInfo", None), "my_node_num", None)
        return send_text(iface, args[0], args[1], timeout, me=me)
    if verb == "nodes":
        me = getattr(getattr(iface, "myInfo", None), "my_node_num", None)
        return nodes_text(getattr(iface, "nodes", None), clock(), me=me)
    if verb == "announce":
        return announce(iface)
    return info_text(iface)
