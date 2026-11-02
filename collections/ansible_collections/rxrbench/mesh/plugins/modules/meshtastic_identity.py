#!/usr/bin/python
"""Read a Meshtastic-vocabulary node's identity key, or give it back one."""

DOCUMENTATION = r"""
module: meshtastic_identity
short_description: A node's identity key, for escrow, and put back from escrow
description:
  - "The node's identity is its X25519 key pair: its node id is derived from the public key
    (zephyrtastic, C(id = crc32(public key))), and peers know it by that key. A board that is
    erased or flashed with another layout generates a new one and is a new node to the mesh.
    This module is the key level of the backups: the key alone, independent of any flash map
    or firmware build, kept in the site's encrypted secrets."
  - "C(action=read) returns the private key (base64) and the node id. Register it with
    C(no_log: true): it is the node's secret."
  - "C(action=ensure) compares the node's private key with C(private_key) and, if they
    differ, writes it through the phone API (C(security) config). The firmware re-derives the
    public key and the node id from it at the next boot; the module waits for the node to
    come back with C(node_id). Check mode reports whether it would write."
  - "The key travels through the board's console tunnel; the broker keeps tunnel payloads
    out of its log. It never appears in this module's output in ensure mode."
  - "The write sends back the C(security) config the node gave, with only the private key
    changed, so its admin keys and its other security config stay as they are. A managed
    node (C(security.is_managed)) drops every local write: one that holds another key is
    refused before anything is sent."
  - "C(transport=serial) or C(tcp) reach a board that runs stock Meshtastic, over its own phone
    API: on its USB serial port (resolved by name, checked with C(devreg guard), held under a
    device lock while the module runs) or on its network API (the address the registry records
    for the route). Before 2.8 a stock node's id does not come from its key (an nRF52's from
    its chip), so C(ensure) refuses a node whose id is neither the escrow's (another board) nor,
    as from 2.8, crc32 of its own public key, writes the key,
    and waits until the node reports that private key; the public key is the firmware's to
    derive."
options:
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode), a read and a board that already matches need none.
    type: bool
    default: false
  board:
    description: The board's name.
    type: str
    required: true
  action:
    description: C(read) the key, or C(ensure) the node holds C(private_key).
    type: str
    choices: [read, ensure]
    default: read
  private_key:
    description: (ensure) The escrowed private key, base64 of 32 bytes.
    type: str
  node_id:
    description: (ensure) The node id the escrowed key gives, as C(0x%08x).
    type: str
  transport:
    description: C(tunnel), the phone API carried over the board's shell, through its broker;
      C(serial), stock Meshtastic's own phone API on the board's serial port; or C(tcp), its
      network API.
    type: str
    choices: [tunnel, serial, tcp]
    default: tunnel
  host:
    description: (tcp) The node's address, as the registry records it for the route.
    type: str
  port:
    description: (tcp) The API's TCP port.
    type: int
    default: 4403
  bearer:
    description: (tcp) The registry route the address belongs to.
    type: str
    default: wifi
  socket:
    description: (tunnel) The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  device:
    description: (serial) The board's name in the device registry. Default is C(board).
    type: str
  devreg:
    description: (serial) The device registry tool on this host, which resolves the board's
      port by name and proves the path is that board.
    type: path
    default: ~/.devreg/devreg.py
  devlock_dir:
    description: (serial) Where devlock.py lives on this host.
    type: path
    default: ~/.devreg
  deadline:
    description: Seconds a console command may take.
    type: float
    default: 20.0
  settle:
    description: Seconds the node has to come back with the new identity.
    type: float
    default: 60.0
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Read the key for escrow
  rxrbench.mesh.meshtastic_identity:
    board: "{{ bench_board }}"
  register: ident
  no_log: true

- name: Read a stock Meshtastic node's key for escrow
  rxrbench.mesh.meshtastic_identity:
    board: "{{ bench_board }}"
    transport: serial
  register: ident
  no_log: true

- name: Give the node its escrowed key back
  rxrbench.mesh.meshtastic_identity:
    board: "{{ bench_board }}"
    action: ensure
    private_key: "{{ (escrow | rxrbench.core.escrow_get('zephyrtastic', 'identity')).value }}"
    node_id: "{{ (escrow | rxrbench.core.escrow_get('zephyrtastic', 'identity')).id }}"
"""

RETURN = r"""
node_id:
  description: The node's id (after the write, in ensure mode).
  returned: always
  type: str
private_key:
  description: (read) The private key, base64. A secret.
  returned: action=read
  type: str
public_key_fp:
  description: crc32 of the public key, as C(%08x) (the node id on zephyrtastic).
  returned: always
  type: str
"""

import base64
import binascii
import os
import sys
import time

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from ansible_collections.rxrbench.mesh.plugins.module_utils import state, stocknode, tunnel


def state_of(b, deadline):
    facts = state.state_facts(b.exec(state.STATE_COMMAND, deadline=deadline))
    if facts is None:
        raise state.StateError("this firmware has no `meshtastic state`")
    return facts


def state_id(b, deadline):
    return state_of(b, deadline).get("node_id")


def keys_of(iface):
    """(private, public) bytes of a connected node, as its phone API gives them locally."""
    sec = iface.localNode.localConfig.security
    return bytes(sec.private_key), bytes(sec.public_key)


def set_private_key(sec, private):
    """Put another private key into the SecurityConfig a node sent, and nothing else: what is
    written back still carries the node's admin keys and its other security config."""
    sec.private_key = private
    sec.public_key = b""        # the firmware derives it from the private key at boot


def read_keys(b, deadline):
    """(private, public) bytes, through the tunnel."""
    iface = tunnel.TunnelInterface(lambda cmd: b.exec(cmd, deadline=deadline), noNodes=True,
                                   poll_interval=0.2)
    try:
        return keys_of(iface)
    finally:
        iface.close()


def write_key(b, deadline, private):
    """Send the key. The node stores it and reboots at once, so the last command's answer may
    never come: that is not a failure. What counts is the id the node comes back with."""
    iface = tunnel.TunnelInterface(lambda cmd: b.exec(cmd, deadline=deadline), noNodes=True,
                                   poll_interval=0.2)
    try:
        node = iface.localNode
        set_private_key(node.localConfig.security, private)
        node.writeConfig("security")
    except (broker_client.BrokerError, tunnel.TunnelError):
        pass
    finally:
        try:
            iface.close()
        except (broker_client.BrokerError, tunnel.TunnelError):
            pass


def go_ahead(module):
    """The one place either write (through the tunnel, or to stock firmware) passes the gate."""
    gate.check(module)   # the layout's go-ahead, before the first write


def stock_session(p):
    """The hold on a stock node for this run (stocknode.Session: guarded, locked, reopened)."""
    return stocknode.Session(p["transport"], p["board"], device=p.get("device") or p["board"],
                             host=p.get("host"), port=p.get("port") or 4403,
                             bearer=p.get("bearer") or "wifi", devreg=p["devreg"],
                             devlock_dir=p["devlock_dir"])


def stock_keys(session, deadline):
    """(node id, private, public, managed) of a stock node, over its phone API."""
    iface = session.open(deadline)
    try:
        info = iface.myInfo
        sec = iface.localNode.localConfig.security
        return (f"0x{info.my_node_num:08x}", bytes(sec.private_key), bytes(sec.public_key),
                bool(sec.is_managed))
    finally:
        iface.close()


def stock_write_key(session, deadline, private, flush=2.0, sleep=time.sleep):
    """Send the key in the node's own security config. The node saves it and may restart."""
    iface = session.open(deadline)
    try:
        node = iface.localNode
        set_private_key(node.localConfig.security, private)
        node.writeConfig("security")
        sleep(flush)
    finally:
        try:
            iface.close()
        except Exception:  # pylint: disable=broad-except
            pass


def stock(module, p, sleep=time.sleep, clock=time.monotonic):
    """Read or ensure the key of a stock node; exits the module."""
    with stock_session(p) as s:
        node_id, private, public, managed = stock_keys(s, p["settle"])
        if p["action"] == "read":
            # stock firmware makes its key only once a region is set: a factory node has none
            if len(private) != 32 or not any(private):
                module.fail_json(msg=f"{p['board']} reports no private key (stock firmware "
                                     "makes one once its region is set)")
            module.exit_json(changed=False, node_id=node_id, public_key_fp=fp(public),
                             private_key=base64.b64encode(private).decode())
        want = base64.b64decode(p["private_key"], validate=True)
        if len(want) != 32:
            module.fail_json(msg="the escrowed private key is not 32 bytes")
        want_id = p["node_id"].lower()
        # Before 2.8 a stock node's id is the device's own (from its MAC or chip), so another id
        # is another board. From 2.8 it is crc32 of the public key, as on zephyrtastic: it says
        # which key the node holds, not which board it is (the port's guard says that), and
        # the escrowed key brings its own id back with it.
        if node_id != want_id and node_id != f"0x{fp(public)}":
            module.fail_json(msg=f"{p['board']} is node {node_id}, the escrowed key is node "
                                 f"{want_id}'s: on stock firmware before 2.8 the id is the "
                                 "device's own, so this is another device (or another "
                                 "firmware's escrow)")
        if want == private:
            module.exit_json(changed=False, node_id=node_id, public_key_fp=fp(public))
        result = {"changed": True, "node_id": node_id}
        if managed:
            module.fail_json(msg=f"{p['board']} is managed (security.is_managed) and holds "
                                 "another key: it takes no local write. Erase its config region first",
                             **result)
        if module.check_mode:
            module.exit_json(msg=f"would give {p['board']} ({node_id}) its escrowed key", **result)
        go_ahead(module)
        stock_write_key(s, p["settle"], want, sleep=sleep)
        end, why, took = clock() + p["settle"], None, None
        while took is None and clock() < end:
            try:
                node_id, private, public, _ = stock_keys(s, max(end - clock(), 1.0))
                if private == want:
                    took = public
                else:
                    why = "it still reports another key"
            except Exception as e:  # pylint: disable=broad-except
                # the node restarts after the write: a refused or cut connection is expected
                why = f"{type(e).__name__}: {e}"
            if took is None:
                sleep(2)
        if took is None:
            module.fail_json(msg=f"{p['board']} did not take the escrowed key ({why})", **result)
        module.exit_json(**{**result, "node_id": node_id}, public_key_fp=fp(took))


def fp(public):
    return f"{binascii.crc32(public) & 0xffffffff:08x}"


def main():
    module = AnsibleModule(
        argument_spec={
            **gate.ARGS,
            "board": {"type": "str", "required": True},
            "action": {"type": "str", "choices": ["read", "ensure"], "default": "read"},
            "private_key": {"type": "str", "no_log": True},
            "node_id": {"type": "str"},
            "transport": {"type": "str", "choices": ["tunnel", "serial", "tcp"], "default": "tunnel"},
            "host": {"type": "str"},
            "port": {"type": "int", "default": 4403},
            "bearer": {"type": "str", "default": "wifi"},
            "socket": {"type": "path"},
            "device": {"type": "str"},
            "devreg": {"type": "path", "default": "~/.devreg/devreg.py"},
            "devlock_dir": {"type": "path", "default": "~/.devreg"},
            "deadline": {"type": "float", "default": 20.0},
            "settle": {"type": "float", "default": 60.0},
        },
        required_if=[("action", "ensure", ("private_key", "node_id"))],
        supports_check_mode=True,
    )
    p = module.params
    if not tunnel.HAS_MESHTASTIC:
        module.fail_json(msg="the `meshtastic` Python library is not installed for this "
                             "module's interpreter")
    if p["transport"] in ("serial", "tcp"):
        try:
            stock(module, p)
        except (state.StateError, stocknode.NodeError, OSError) as e:
            module.fail_json(msg=str(e))
        except Exception as e:  # pylint: disable=broad-except
            # The stock library raises its own types (a port that does not answer).
            module.fail_json(msg=f"{p['board']}: {type(e).__name__}: {e}")
    try:
        with broker_client.Board(p["board"], path=p["socket"]) as b:
            facts = state_of(b, p["deadline"])
            node_id = facts.get("node_id")
            private, public = read_keys(b, p["deadline"])
            if len(private) != 32 or not any(private):
                raise state.StateError(f"{p['board']} reports no private key")
            if p["action"] == "read":
                module.exit_json(changed=False, node_id=node_id, public_key_fp=fp(public),
                                 private_key=base64.b64encode(private).decode())
            want = base64.b64decode(p["private_key"], validate=True)
            if len(want) != 32:
                module.fail_json(msg="the escrowed private key is not 32 bytes")
            want_id = p["node_id"].lower()
            if want == private:
                if node_id != want_id:
                    module.fail_json(msg=f"{p['board']} holds the escrowed key but reports id "
                                         f"{node_id}, the escrow says {want_id}")
                module.exit_json(changed=False, node_id=node_id, public_key_fp=fp(public))
            result = {"changed": True, "node_id_before": node_id, "node_id": want_id}
            if facts.get("managed"):
                module.fail_json(msg=f"{p['board']} is managed (security.is_managed) and holds "
                                     f"another key ({node_id}, the escrow says {want_id}): it "
                                     "takes no local write. Erase its config region first", **result)
            if module.check_mode:
                module.exit_json(msg=f"would give {p['board']} its escrowed key: {node_id} -> "
                                     f"{want_id}", **result)
            go_ahead(module)
            write_key(b, p["deadline"], want)
            end = time.monotonic() + p["settle"]
            seen, why = None, None
            while time.monotonic() < end:
                try:
                    seen = state_id(b, p["deadline"])
                    if seen == want_id:
                        break
                except (broker_client.BrokerError, state.StateError) as e:
                    why = str(e)
                time.sleep(1.0)
            if seen != want_id:
                module.fail_json(msg=f"{p['board']} did not come back as {want_id} (last seen "
                                     f"{seen}{'; ' + why if why else ''})", **result)
            private, public = read_keys(b, p["deadline"])
            if private != want:
                module.fail_json(msg=f"{p['board']} came back as {want_id} but its private key "
                                     f"is not the escrowed one", **result)
            result["public_key_fp"] = fp(public)
    except (broker_client.BrokerError, state.StateError, tunnel.TunnelError, ValueError,
            binascii.Error) as e:
        module.fail_json(msg=str(e))
    module.exit_json(**result)


if __name__ == "__main__":
    main()
