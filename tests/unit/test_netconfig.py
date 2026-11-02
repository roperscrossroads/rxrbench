"""Stock Meshtastic over TCP: facts read from the phone API in config.plan()'s terms, the
display section, the write's transaction and the module's settle loop, on fake interfaces
built from the real protobufs (no node, no network)."""
import pytest

pytest.importorskip("meshtastic")

from meshtastic.protobuf import channel_pb2, config_pb2, localonly_pb2  # noqa: E402

from ansible_collections.rxrbench.mesh.plugins.module_utils import config, netconfig, state  # noqa: E402
from ansible_collections.rxrbench.mesh.plugins.modules import meshtastic_net_config as mod  # noqa: E402

SECRET = bytes(range(16))


def channel(index, role, name="", psk=b"\x01"):
    ch = channel_pb2.Channel(index=index, role=role)
    ch.settings.name = name
    ch.settings.psk = psk
    return ch


class Node:
    def __init__(self):
        self.localConfig = localonly_pb2.LocalConfig()
        self.localConfig.device.role = config_pb2.Config.DeviceConfig.Role.Value("CLIENT")
        self.localConfig.lora.hop_limit = 3
        self.localConfig.display.displaymode = config_pb2.Config.DisplayConfig.DisplayMode.Value("DEFAULT")
        self.localConfig.display.screen_on_secs = 30
        self.channels = [channel(0, 1, "", b"\x01"), channel(1, 2, "team", SECRET),
                         channel(2, 2, "spare", state.DEFAULT_PSK), channel(3, 0)]
        self.calls = []

    def beginSettingsTransaction(self):
        self.calls.append("begin")

    def commitSettingsTransaction(self):
        self.calls.append("commit")

    def writeConfig(self, name):
        self.calls.append(f"config:{name}")

    def writeChannel(self, i):
        self.calls.append(f"channel:{i}")

    def setOwner(self, long_name, short_name):
        self.calls.append(f"owner:{long_name}/{short_name}")


class Iface:
    def __init__(self, node=None):
        self.localNode = node or Node()
        self.metadata = type("M", (), {"firmware_version": "2.8.2.abc"})()

    def getMyNodeInfo(self):
        return {"user": {"id": "!0a0b0c0d", "longName": "m9", "shortName": "m9"}}

    def close(self):
        pass


def test_channels_read_as_the_state_line_shows_them():
    f = netconfig.facts_of(Iface())
    chans = {c["index"]: c for c in f["channels"]}
    assert set(chans) == {0, 1, 2}, "a disabled slot is not listed"
    assert chans[0]["psk"] == "default" and not chans[0]["expanded"]
    assert chans[1] == {"index": 1, "name": "team", "role": "SECONDARY", "uplink_enabled": False,
                        "downlink_enabled": False, "psk": "secret",
                        "fingerprint": state.key_fingerprint(SECRET)}
    assert chans[2]["psk"] == "default" and chans[2]["expanded"]
    assert all(SECRET.hex() not in repr(v) for v in f.values()), "no key in the facts"


def test_facts_shape():
    f = netconfig.facts_of(Iface())
    assert f["owner"] == {"long_name": "m9", "short_name": "m9"} and f["node_id"] == "!0a0b0c0d"
    assert f["device"]["role"] == "CLIENT" and f["lora_api"] == {"hop_limit": 3}
    assert f["display"] == {"displaymode": "DEFAULT", "screen_on_secs": 30}
    assert f["managed"] is False and f["firmware"] == "2.8.2.abc"


def test_a_node_at_its_declared_state_plans_nothing():
    f = netconfig.facts_of(Iface())
    changes, _ = netconfig.plan({"display": {"displaymode": "DEFAULT"}, "owner": {"long_name": "m9"},
                                 "channels": [{"index": 1, "role": "SECONDARY", "name": "team",
                                               "psk": "hex:" + SECRET.hex()}]}, f)
    assert changes == {}


def test_the_display_is_planned_and_shown_in_the_diff():
    f = netconfig.facts_of(Iface())
    changes, diff = netconfig.plan({"display": {"displaymode": "COLOR"}}, f)
    assert changes == {"display": {"displaymode": "COLOR"}}
    assert diff == {"before": {"display": {"displaymode": "DEFAULT"}},
                    "after": {"display": {"displaymode": "COLOR"}}}


def test_a_bad_display_value_is_refused():
    f = netconfig.facts_of(Iface())
    with pytest.raises(config.ConfigError):
        netconfig.plan({"display": {"displaymode": "RAINBOW"}}, f)
    with pytest.raises(config.ConfigError):
        netconfig.plan({"display": {"brightness": 3}}, f)


def test_the_display_rides_the_one_transaction():
    iface = Iface()
    config.write(iface, {"display": {"displaymode": "COLOR"}, "device": {"role": "CLIENT_MUTE"}})
    node = iface.localNode
    assert node.calls == ["begin", "config:device", "config:display", "commit"]
    assert node.localConfig.display.displaymode == \
        config_pb2.Config.DisplayConfig.DisplayMode.Value("COLOR")


def test_settle_waits_out_a_restart(monkeypatch):
    """The node refuses the connection while it restarts, then shows the write."""
    reads = iter([ConnectionRefusedError("down"), ConnectionRefusedError("down"), "done"])
    node = Node()

    def fake_read(_p):
        r = next(reads)
        if isinstance(r, Exception):
            raise r
        node.localConfig.display.displaymode = config_pb2.Config.DisplayConfig.DisplayMode.Value("COLOR")
        return netconfig.facts_of(Iface(node))

    monkeypatch.setattr(mod, "read", fake_read)
    t = [0.0]
    facts, left, why = mod.settle({"settle": 60}, {"display": {"displaymode": "COLOR"}},
                                  {"display": {"displaymode": "COLOR"}},
                                  sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert left == {} and why is None and facts["display"]["displaymode"] == "COLOR"


def test_settle_gives_up_with_the_last_error(monkeypatch):
    def fake_read(_p):
        raise TimeoutError("no answer")

    monkeypatch.setattr(mod, "read", fake_read)
    t = [0.0]
    facts, left, why = mod.settle({"settle": 10}, {"display": {"displaymode": "COLOR"}},
                                  {"display": {"displaymode": "COLOR"}},
                                  sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert facts is None and left and "TimeoutError" in why


def test_a_switch_to_color_is_written_last_and_alone():
    steps = netconfig.phases({"display": {"displaymode": "COLOR", "screen_on_secs": 60},
                              "device": {"role": "CLIENT"}, "security": {"admin_key": []}})
    assert steps == [{"device": {"role": "CLIENT"}, "display": {"screen_on_secs": 60}},
                     {"security": {"admin_key": []}},
                     {"display": {"displaymode": "COLOR"}}]
    assert netconfig.phases({"display": {"displaymode": "DEFAULT"}}) == \
        [{"display": {"displaymode": "DEFAULT"}}], "the button UI keeps the API: no reordering"


def test_color_and_becoming_managed_in_one_run_is_refused():
    with pytest.raises(config.ConfigError):
        netconfig.phases({"display": {"displaymode": "COLOR"}, "security": {"is_managed": True}})


def _clocked():
    t = [0.0]
    return t, (lambda s: t.__setitem__(0, t[0] + s)), (lambda: t[0])


def test_settle_closing_takes_a_closed_api_as_done(monkeypatch):
    """The node answers with its old state, restarts, and never serves the API again."""
    reads = iter([netconfig.facts_of(Iface())])

    def fake_read(_p):
        r = next(reads, None)
        if r is None:
            raise ConnectionRefusedError("MUI: no API")
        return r

    monkeypatch.setattr(mod, "read", fake_read)
    t, sleep, clock = _clocked()
    want = {"display": {"displaymode": "COLOR"}}
    facts, left, why, closed = mod.settle_closing({"settle": 120, "api_off_grace": 45}, want, want,
                                                  sleep=sleep, clock=clock)
    assert closed and left == {} and why is None
    assert facts["display"]["displaymode"] == "DEFAULT", "the last thing read is what is returned"
    assert t[0] < 60


def test_settle_closing_reports_a_node_that_came_back_without_the_ui(monkeypatch):
    """A build without MUI coerces COLOR back to DEFAULT: it answers, and the switch did not take."""
    reads = iter([ConnectionRefusedError("restarting")] * 3)

    def fake_read(_p):
        r = next(reads, None)
        if r is not None:
            raise r
        return netconfig.facts_of(Iface())

    monkeypatch.setattr(mod, "read", fake_read)
    _, sleep, clock = _clocked()
    want = {"display": {"displaymode": "COLOR"}}
    facts, left, _why, closed = mod.settle_closing({"settle": 60, "api_off_grace": 45}, want, want,
                                                   sleep=sleep, clock=clock)
    assert not closed and left == want and facts["display"]["displaymode"] == "DEFAULT"
