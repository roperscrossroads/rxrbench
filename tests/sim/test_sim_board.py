"""The real firmware, as a process, under the real broker code and the real meshtastic
library. Needs the sim board built (`just sim-build`); skipped without it.

The unit tests run the client against a model of the firmware (tests/support/fake_api.py),
and a model agrees with whoever wrote it: the firmware's QueueStatus bug sat behind a model
that answered `free=16`. These tests have no model in them.
"""
import importlib.util
import os
import time

import pytest

from ansible_collections.rxrbench.mesh.plugins.module_utils import state, tunnel
from ansible_collections.rxrbench.mesh.sim import simboard as sim_board

pytestmark = pytest.mark.skipif(not sim_board.available(),
                                reason="sim board not built (just sim-build)")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("rxrbench_broker",
                                              os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "broker.py"))
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)


class Rig:
    """A sim board and the broker's console on it."""

    def __init__(self, tmp_path):
        self.sim = sim_board.SimBoard(tmp_path).start()
        self.con = mb.Console("sim1", mb.path_opener(self.sim.path), str(tmp_path / "sim1.log"))

    def run(self, cmd, deadline=10.0):
        """One shell command, as the broker runs it: the port is reopened when the board has
        been away, as the broker's main loop would."""
        end = time.monotonic() + 10
        while not self.con.is_open():
            self.con.next_open = 0.0
            self.con.tick()
            assert time.monotonic() < end, f"no console: {self.con.last_error}"
            time.sleep(0.05)
        r = self.con.exec(cmd, deadline=deadline)
        assert r.get("ok"), r
        return r["output"]

    def facts(self):
        return state.state_facts(self.run(state.STATE_COMMAND))

    def library(self, **kw):
        return tunnel.TunnelInterface(self.run, poll_interval=0.1, connect_timeout=20, **kw)

    def reboot(self):
        self.con.close("the board is rebooting")
        self.sim.reboot()

    def close(self):
        self.con.close("test over")
        self.sim.stop()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def test_the_board_answers_as_a_bench_board_does(rig):
    assert "board:  native_sim" in rig.run("meshtastic version")
    f = rig.facts()
    assert f["state_supported"] is True
    assert f["board"] == "native_sim" and "phoneapi" in f["features"]
    assert f["channels"][0]["role"] == "PRIMARY"


def test_a_fresh_board_s_primary_holds_the_default_key_in_its_short_form(rig):
    """The form the client apps show without a lock. The defaults hold it in short
    form; an init that expands it to the 16 bytes would show up here."""
    primary = rig.facts()["channels"][0]
    assert primary["psk"] == "default" and primary["expanded"] is False


def test_the_stock_library_reads_the_real_firmware(rig):
    f = rig.facts()
    iface = rig.library()
    try:
        assert f"0x{iface.myInfo.my_node_num:08x}" == f["node_id"]
        assert iface.getLongName() == f["owner"]["long_name"]
        lora = iface.localNode.localConfig.lora
        assert lora.tx_power == f["lora"]["tx_power"]
        assert lora.tx_enabled == f["lora"]["tx_enabled"]
        assert iface.localNode.channels[0].role == 1
    finally:
        iface.close()


def test_a_write_through_the_library_lands_and_survives_a_reboot(rig):
    iface = rig.library()
    try:
        iface.localNode.setOwner("written through the tunnel", "wtt")
    finally:
        iface.close()
    assert rig.facts()["owner"] == {"long_name": "written through the tunnel",
                                    "short_name": "wtt"}
    time.sleep(1.5)     # the firmware saves on a short delay
    rig.reboot()
    assert rig.sim.boots == 2
    assert rig.facts()["owner"]["long_name"] == "written through the tunnel"


def test_a_channel_with_a_key_is_written_and_only_its_fingerprint_is_shown(rig):
    key = bytes(range(32))
    iface = rig.library()
    try:
        ch = iface.localNode.channels[1]
        ch.role = 2                      # SECONDARY
        ch.settings.name = "side"
        ch.settings.psk = key
        iface.localNode.writeChannel(1)
    finally:
        iface.close()
    side = [c for c in rig.facts()["channels"] if c["index"] == 1]
    assert side and side[0]["name"] == "side" and side[0]["role"] == "SECONDARY"
    assert side[0]["psk"] == "secret"
    assert side[0]["fingerprint"] == state.key_fingerprint(key)
    assert key.hex() not in rig.run(state.STATE_COMMAND)


def test_the_queue_status_the_library_obeys_leaves_room_to_send(rig):
    """The queue status must leave room to send: the library obeys it, and a status that
    reports the phone-side queue (full on a node nobody reads) stops it sending at all."""
    time.sleep(1.0)                     # let boot-time frames fill the transport's queue
    iface = rig.library()
    try:
        assert iface.queueStatus is not None, "the config stream carries a QueueStatus"
        assert iface.queueStatus.free > 0
        assert iface.queueStatus.maxlen >= iface.queueStatus.free
    finally:
        iface.close()


def test_a_radio_config_write_is_stored(rig):
    iface = rig.library()
    try:
        lora = iface.localNode.localConfig.lora
        lora.tx_power = 2
        lora.modem_preset = 6            # SHORT_FAST
        iface.localNode.writeConfig("lora")
    finally:
        iface.close()
    end = time.monotonic() + 15
    f = None
    while time.monotonic() < end:
        try:
            f = rig.facts()
            if f["lora"]["tx_power"] == 2:
                break
        except AssertionError:
            pass                         # the board may be rebooting to apply it
        time.sleep(0.3)
    assert f is not None and f["lora"]["tx_power"] == 2
    assert f["lora"]["modem_preset"] == "SHORT_FAST"
    assert f["lora"]["live_preset"] == "SHORT_FAST", "applied to the radio, not only stored"
