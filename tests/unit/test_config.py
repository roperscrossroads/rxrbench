import base64

import pytest

from ansible_collections.rxrbench.mesh.plugins.module_utils import config, state

KEY = bytes(range(16))
FACTS = {
    "owner": {"long_name": "Zephyr Meshtastic", "short_name": "0101"},
    "device": {"role": "CLIENT", "rebroadcast_mode": "NONE"},
    "channels": [
        {"index": 0, "name": "LongFast", "hash": 8, "role": "PRIMARY", "uplink_enabled": True,
         "downlink_enabled": True, "psk": "default", "expanded": False},
        {"index": 2, "name": "cluster", "hash": 224, "role": "SECONDARY", "uplink_enabled": False,
         "downlink_enabled": False, "psk": "secret", "fingerprint": state.key_fingerprint(KEY)},
    ],
}
B64 = "base64:" + base64.b64encode(KEY).decode()


def test_a_board_that_is_as_declared_has_nothing_to_write():
    want = {"owner": {"long_name": "Zephyr Meshtastic", "short_name": "0101"},
            "device": {"role": "CLIENT", "rebroadcast_mode": "NONE"},
            "channels": [{"index": 0, "name": "LongFast", "role": "PRIMARY", "psk": "default"},
                         {"index": 2, "name": "cluster", "role": "SECONDARY", "psk": B64}]}
    assert config.plan(want, FACTS) == ({}, {"before": {}, "after": {}})


def test_only_what_differs_is_written_and_a_key_is_shown_as_its_fingerprint():
    other = "hex:" + bytes(range(1, 17)).hex()
    want = {"owner": {"long_name": "sim1", "short_name": "sim1"},
            "device": {"role": "CLIENT", "rebroadcast_mode": "ALL"},
            "channels": [{"index": 0, "name": "ShortFast", "role": "PRIMARY", "psk": "default"},
                         {"index": 2, "name": "cluster", "role": "SECONDARY", "psk": other}]}
    changes, diff = config.plan(want, FACTS)
    assert changes["owner"] == {"long_name": "sim1", "short_name": "sim1"}
    assert changes["device"] == {"rebroadcast_mode": "ALL"}, "the role already matches"
    assert [c["index"] for c in changes["channels"]] == [0, 2]
    assert diff["after"]["channels"][2]["psk"].startswith("secret:")
    assert bytes(range(1, 17)).hex() not in repr(diff), "the key is in the plan, never the diff"
    assert diff["before"]["channels"][0]["name"] == "LongFast"


def test_an_undeclared_slot_is_left_alone_and_a_declared_disabled_one_is_disabled():
    want = {"channels": [{"index": 2, "role": "DISABLED"}]}
    changes, diff = config.plan(want, FACTS)
    assert changes == {"channels": [{"index": 2, "role": "DISABLED"}]}
    assert diff["after"]["channels"] == {2: {"role": "DISABLED"}}
    # slot 1 is already off: declaring it disabled changes nothing
    assert config.plan({"channels": [{"index": 1, "role": "DISABLED"}]}, FACTS)[0] == {}


def test_the_default_key_stored_as_16_bytes_is_rewritten_in_its_short_form():
    facts = {**FACTS, "channels": [{**FACTS["channels"][0], "expanded": True}]}
    want = {"channels": [{"index": 0, "name": "LongFast", "role": "PRIMARY", "psk": "default"}]}
    changes, diff = config.plan(want, facts)
    assert len(changes["channels"]) == 1
    assert diff["before"]["channels"][0]["psk"] == "well-known key (stored as 16 bytes)"
    assert diff["after"]["channels"][0]["psk"] == "well-known key"


def test_a_new_secondary_channel_is_a_change_from_disabled():
    want = {"channels": [{"index": 3, "name": "health", "role": "SECONDARY", "psk": B64}]}
    changes, diff = config.plan(want, FACTS)
    assert changes["channels"][0]["index"] == 3
    assert diff["before"]["channels"][3] == {"role": "DISABLED"}


def test_part_of_an_owner_is_enough():
    changes, _ = config.plan({"owner": {"long_name": "new"}}, FACTS)
    assert changes["owner"] == {"long_name": "new", "short_name": "0101"}


@pytest.mark.parametrize("bad, why", [
    ({"owner": {"long_name": "  "}}, "non-empty"),
    ({"owner": {"short_name": "toolong"}}, "at most 4"),
    ({"device": {"role": "KING"}}, "not one of"),
    ({"device": {"node_info_broadcast_secs": 60}}, "does not manage"),
    ({"channels": [{"index": 8, "role": "SECONDARY"}]}, "0..7"),
    ({"channels": [{"index": 1, "role": "SECONDARY"}, {"index": 1, "role": "DISABLED"}]}, "twice"),
    ({"channels": [{"index": 0, "role": "PRIMARY"}, {"index": 1, "role": "PRIMARY"}]}, "more than one"),
    ({"channels": [{"index": 1, "role": "SECONDARY", "psk": "hex:0011"}]}, "16 or 32"),
    ({"channels": [{"index": 1, "role": "SECONDARY", "psk": "letmein"}]}, "must be default"),
    ({"channels": [{"index": 1, "role": "SECONDARY", "psk": "base64:@@"}]}, "does not decode"),
    ({"channels": [{"index": 1, "role": "SECONDARY", "name": "a name too long"}]}, "11 bytes"),
    ({"channels": [{"index": 1, "role": "SECONDARY", "colour": "red"}]}, "unknown keys"),
])
def test_a_declaration_that_cannot_be_written_is_refused_before_anything_is_sent(bad, why):
    with pytest.raises(config.ConfigError, match=why):
        config.plan(bad, FACTS)


def test_keys_decode_to_what_a_channel_stores():
    assert config.key_bytes("default") == b"\x01" and config.key_bytes("none") == b""
    assert config.key_bytes(B64) == KEY == config.key_bytes("hex:" + KEY.hex())


def test_a_hop_limit_the_phone_api_shows_is_written_only_when_it_differs():
    facts = {**FACTS, "lora_api": {"hop_limit": 3}}
    changes, diff = config.plan({"lora": {"hop_limit": 6}}, facts)
    assert changes == {"lora": {"hop_limit": 6}}
    assert diff == {"before": {"lora": {"hop_limit": 3}}, "after": {"lora": {"hop_limit": 6}}}
    assert config.plan({"lora": {"hop_limit": 3}}, facts)[0] == {}


@pytest.mark.parametrize("bad", [0, 8, True, "6"])
def test_a_hop_limit_outside_one_to_seven_is_refused(bad):
    with pytest.raises(config.ConfigError):
        config.plan({"lora": {"hop_limit": bad}}, FACTS)


def test_a_lora_field_the_shell_owns_is_not_managed_here():
    with pytest.raises(config.ConfigError):
        config.plan({"lora": {"tx_power": 2}}, FACTS)


# ---- security: admin keys and is_managed --------------------------------------------------

ADMIN1 = "base64:" + base64.b64encode(bytes(range(32))).decode()
ADMIN2 = "hex:" + bytes(range(1, 33)).hex()
ADMIN2_VIEW = "base64:" + base64.b64encode(bytes(range(1, 33))).decode()


def sec_facts(keys=(), managed=False):
    return {**FACTS, "managed": managed, "security_api": {"admin_key": list(keys), "is_managed": managed}}


def test_admin_keys_are_a_set_the_node_must_hold_exactly():
    want = {"security": {"admin_key": [ADMIN1, ADMIN2]}}
    changes, diff = config.plan(want, sec_facts())
    assert changes == {"security": {"admin_key": [ADMIN1, ADMIN2_VIEW]}}
    assert diff["before"]["security"] == {"admin_key": []}
    # the same keys in another order, or in the other spelling, are no difference
    assert config.plan(want, sec_facts([ADMIN2_VIEW, ADMIN1]))[0] == {}
    # one the layout does not list is removed: the declared list is the whole list
    assert config.plan({"security": {"admin_key": [ADMIN1]}}, sec_facts([ADMIN1, ADMIN2_VIEW]))[0] \
        == {"security": {"admin_key": [ADMIN1]}}
    assert config.plan({"security": {"admin_key": []}}, sec_facts([ADMIN1]))[0] \
        == {"security": {"admin_key": []}}


def test_a_layout_that_declares_no_security_leaves_a_nodes_admin_keys_alone():
    assert config.plan({"owner": {"long_name": "Zephyr Meshtastic"}}, sec_facts([ADMIN1], True))[0] == {}
    assert config.plan({"security": {"is_managed": False}}, sec_facts([ADMIN1]))[0] == {}


@pytest.mark.parametrize("bad, why", [
    ({"is_managed": True}, "needs at least one"),
    ({"is_managed": True, "admin_key": []}, "needs at least one"),
    ({"admin_key": [ADMIN1] * 2}, "twice"),
    ({"admin_key": ["hex:" + f"{i:02x}" * 32 for i in range(4)]}, "at most 3"),
    ({"admin_key": ["hex:0011"]}, "32-byte"),
    ({"admin_key": ["AAAA"]}, "base64:<key> or hex:<key>"),
    ({"admin_key": ADMIN1}, "a list"),
    ({"is_managed": "yes"}, "true or false"),
    ({"private_key": "x"}, "does not manage security.private_key"),
])
def test_a_security_declaration_that_locks_out_or_cannot_be_written_is_refused(bad, why):
    with pytest.raises(config.ConfigError, match=why):
        config.plan({"security": bad}, sec_facts())


def test_making_a_node_managed_is_the_last_write_and_by_itself():
    want = {"owner": {"long_name": "sim1", "short_name": "sim1"},
            "security": {"admin_key": [ADMIN1], "is_managed": True}}
    changes, _ = config.plan(want, sec_facts())
    first, last = config.phases(changes)
    assert set(first) == {"owner"}
    assert last == {"security": {"admin_key": [ADMIN1], "is_managed": True}}
    assert config.phases({"owner": {}}) == [{"owner": {}}]


def test_a_managed_node_that_differs_is_refused_and_one_that_matches_is_not():
    want = {"owner": {"long_name": "sim1", "short_name": "sim1"},
            "security": {"admin_key": [ADMIN1], "is_managed": True}}
    facts = sec_facts([ADMIN1], managed=True)
    changes, _ = config.plan(want, facts)
    assert set(changes) == {"owner"}
    assert "cannot change its owner" in config.managed_refusal(changes, facts)
    # nothing local unmanages it either
    off, _ = config.plan({"security": {"is_managed": False}}, facts)
    assert off == {"security": {"is_managed": False}}
    assert config.managed_refusal(off, facts)
    same = {**facts, "owner": {"long_name": "sim1", "short_name": "sim1"}}
    assert config.managed_refusal(config.plan(want, same)[0], same) is None
    assert config.managed_refusal(changes, sec_facts([ADMIN1])) is None


class _Sec:
    """A SecurityConfig as the stock library holds it, as far as the writer touches it."""
    def __init__(self, admin=(), managed=False):
        self.private_key, self.public_key = b"\x07" * 32, b"\x08" * 32
        self.admin_key, self.is_managed, self.serial_enabled = list(admin), managed, True


class _Node:
    def __init__(self, sec):
        self.localConfig = type("C", (), {"security": sec})()
        self.sent = []

    def writeConfig(self, name):
        self.sent.append(name)

    def beginSettingsTransaction(self):
        self.sent.append("begin")

    def commitSettingsTransaction(self):
        self.sent.append("commit")


def test_the_security_write_keeps_the_nodes_key_pair_and_opens_no_transaction():
    sec = _Sec(admin=[b"\xaa" * 32])
    node = _Node(sec)
    config.write(type("I", (), {"localNode": node})(),
                 {"security": {"admin_key": [ADMIN1, ADMIN2], "is_managed": True}})
    assert node.sent == ["security"], "no begin, no commit: a managed node drops the commit"
    assert sec.admin_key == [bytes(range(32)), bytes(range(1, 33))]
    assert sec.is_managed is True
    assert (sec.private_key, sec.public_key, sec.serial_enabled) == (b"\x07" * 32, b"\x08" * 32, True)
    assert config.security_api(sec) == {"admin_key": [ADMIN1, ADMIN2_VIEW], "is_managed": True}
    with pytest.raises(config.ConfigError, match="by itself"):
        config.write(type("I", (), {"localNode": node})(), {"owner": {}, "security": {}})


def test_the_restart_after_a_security_write_is_waited_out():
    from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
    from ansible_collections.rxrbench.mesh.plugins.modules import meshtastic_config as mc

    class Board:
        def __init__(self, answers):
            self.answers = list(answers)

        def exec(self, cmd, deadline=None):
            a = self.answers.pop(0) if self.answers else "up"
            if a == "down":
                raise broker_client.BrokerError("port closed")
            return ""            # no state line: read as "no state", which is an answer

    now = [0.0]
    tick = lambda s: now.__setitem__(0, now[0] + s)  # noqa: E731
    assert mc.await_restart(Board(["up", "up", "down", "down", "up"]), 1, 20, tick, lambda: now[0]) is True
    assert now[0] == 2.0, "it returns as soon as the board answers again"
    now[0] = 0.0
    assert mc.await_restart(Board([]), 1, 3, tick, lambda: now[0]) is False, "none seen: go on"



def test_every_admin_message_of_a_transaction_is_paced(monkeypatch):
    """Stock firmware drops the oldest of a burst from the phone API (meshtasticd 2.7.26 lost an
    edit transaction's begin and owner write): the writer leaves PACE after each message."""
    monkeypatch.setattr(config, "PACE", 0.5)
    order = []
    node = type("N", (), {})()
    node.beginSettingsTransaction = lambda: order.append("begin")
    node.commitSettingsTransaction = lambda: order.append("commit")
    node.setOwner = lambda long_name, short_name: order.append("owner")
    node.writeConfig = lambda name: order.append(name)
    node.localConfig = type("C", (), {"lora": type("L", (), {})()})()
    config.write(type("I", (), {"localNode": node})(),
                 {"owner": {"long_name": "a", "short_name": "b"}, "lora": {"hop_limit": 4}},
                 sleep=lambda s: order.append(f"wait {s}"))
    assert order == ["begin", "wait 0.5", "owner", "wait 0.5", "lora", "wait 0.5", "commit"]


def test_a_restart_seen_only_as_the_broker_reopening_the_port_counts():
    """Since a command reopens a closed port at once (29ba939), a restart between two reads may
    fail none of them: the broker's count of opens going up is the sign (the simulated
    managed-node test caught it)."""
    from ansible_collections.rxrbench.mesh.plugins.modules import meshtastic_config as mc

    class Board:
        def __init__(self):
            self.n, self.reads = 1, 0

        def status(self):
            return {"ok": True, "opens": self.n}

        def exec(self, cmd, deadline=None):
            self.reads += 1
            if self.reads == 3:
                self.n += 1      # it restarted and the broker reopened the port for this read
            return ""

    now = [0.0]
    tick = lambda s: now.__setitem__(0, now[0] + s)  # noqa: E731
    assert mc.await_restart(Board(), 1, 20, tick, lambda: now[0]) is True
