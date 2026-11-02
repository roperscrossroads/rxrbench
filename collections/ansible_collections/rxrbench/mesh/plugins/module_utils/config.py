"""Owner, device, channel and security config: what differs between a layout and a board.

`plan()` is pure: the declared state and the facts from `meshtastic state` in, the changes
out. The read side needs no client library (the state line has everything, with keys as
fingerprints) except for the fields only the phone API shows (`lora_api`, `security_api`);
a write always goes through the phone API.

Only what is declared is managed. A channel slot the layout does not list is left alone, and
so are the admin keys of a board whose layout declares none.

A managed node (`security.is_managed`) takes configuration only from a remote admin: the
firmware drops every local write, this tool's included, and nothing local can switch it off
again. So `is_managed: true` is the last thing written, by itself, after everything else has
been read back; and a managed board that differs from its layout is refused, not written.
"""
import base64
import binascii
import time

from . import enums, state

DEVICE_FIELDS = {"role": enums.DEVICE_ROLE, "rebroadcast_mode": enums.REBROADCAST_MODE}
# LoRa fields the phone API carries and the shell does not (the shell owns preset, power and
# the transmitter: radio.py). Read and written through the tunnel.
LORA_FIELDS = {"hop_limit"}
CHANNEL_ROLES = set(enums.CHANNEL_ROLE.values())
CHANNEL_KEYS = {"index", "name", "role", "psk", "uplink_enabled", "downlink_enabled"}
MAX_CHANNELS = 8
# The security fields managed here, by their stock names. The identity key pair is not among
# them: it is the device's own secret (meshtastic_identity).
SECURITY_FIELDS = {"admin_key", "is_managed"}
MAX_ADMIN_KEYS = 3
ADMIN_KEY_LEN = 32


class ConfigError(ValueError):
    pass


def key_bytes(psk):
    """A layout's `psk` as the bytes a channel stores.

    `default` is the well-known key in its one-byte form, `none` is no encryption, and a real
    key is `base64:<...>` (the stock export's form) or `hex:<...>`, 16 or 32 bytes.
    """
    if psk == "default":
        return b"\x01"
    if psk == "none":
        return b""
    if not isinstance(psk, str):
        raise ConfigError("a channel's psk must be default, none, base64:<key> or hex:<key>")
    try:
        if psk.startswith("base64:"):
            key = base64.b64decode(psk[7:], validate=True)
        elif psk.startswith("hex:"):
            key = bytes.fromhex(psk[4:])
        else:
            raise ConfigError("a channel's psk must be default, none, base64:<key> or "
                              "hex:<key>")
    except (binascii.Error, ValueError) as e:
        if isinstance(e, ConfigError):
            raise
        raise ConfigError("a channel's psk does not decode") from e
    if len(key) not in (16, 32):
        raise ConfigError(f"a channel key is 16 or 32 bytes, this one is {len(key)}")
    return key


def admin_key_bytes(key):
    """A layout's admin key (a public key: `base64:<...>`, the stock export's form, or
    `hex:<...>`) as its 32 bytes."""
    if not isinstance(key, str):
        raise ConfigError("an admin key must be base64:<key> or hex:<key>")
    try:
        if key.startswith("base64:"):
            raw = base64.b64decode(key[7:], validate=True)
        elif key.startswith("hex:"):
            raw = bytes.fromhex(key[4:])
        else:
            raise ConfigError("an admin key must be base64:<key> or hex:<key>")
    except (binascii.Error, ValueError) as e:
        if isinstance(e, ConfigError):
            raise
        raise ConfigError("an admin key does not decode") from e
    if len(raw) != ADMIN_KEY_LEN:
        raise ConfigError(f"an admin key is a {ADMIN_KEY_LEN}-byte public key, this one is "
                          f"{len(raw)} bytes")
    return raw


def admin_key_view(raw):
    """An admin key as a diff shows it. It is a public key, shown as the apps show it."""
    return "base64:" + base64.b64encode(bytes(raw)).decode()


def check_security(sec):
    """Refuse a security declaration that cannot be written, or that would lock every admin
    out: a managed node obeys only the holders of its admin keys."""
    sec = sec or {}
    extra = set(sec) - SECURITY_FIELDS
    if extra:
        raise ConfigError(f"meshtastic_config does not manage security.{sorted(extra)[0]}")
    keys = sec.get("admin_key")
    if keys is not None:
        if not isinstance(keys, (list, tuple)):
            raise ConfigError("security.admin_key is a list of public keys")
        if len(keys) > MAX_ADMIN_KEYS:
            raise ConfigError(f"a node holds at most {MAX_ADMIN_KEYS} admin keys, "
                              f"{len(keys)} are declared")
        raw = [admin_key_bytes(k) for k in keys]
        if len(set(raw)) != len(raw):
            raise ConfigError("security.admin_key lists one key twice")
    managed = sec.get("is_managed")
    if managed is not None and not isinstance(managed, bool):
        raise ConfigError(f"security.is_managed is true or false, got {managed!r}")
    if managed and not keys:
        raise ConfigError("security.is_managed needs at least one security.admin_key declared "
                          "beside it: a managed node with no admin key obeys nobody")


# How a key reads in a diff: never the key, and never the layout's own words for it. The
# module's psk parameter is no_log, and Ansible blanks every occurrence of a no_log value
# in its output, "default" included.
WELL_KNOWN = "well-known key"
UNENCRYPTED = "unencrypted"


def key_view(psk):
    """A declared key as a diff shows it."""
    key = key_bytes(psk)
    if key == b"":
        return UNENCRYPTED
    if key == b"\x01":
        return WELL_KNOWN
    return "secret:" + state.key_fingerprint(key)


def _actual_key_view(ch):
    if ch["psk"] == "secret":
        return "secret:" + ch["fingerprint"]
    if ch["psk"] == "none":
        return UNENCRYPTED
    if ch["psk"] == "default":
        # Stored as its 16 bytes it is the same key on the air, but a lock in the apps.
        return WELL_KNOWN + (" (stored as 16 bytes)" if ch.get("expanded") else "")
    return WELL_KNOWN + " " + ch["psk"][len("default"):]


def check(desired):
    owner = desired.get("owner") or {}
    for k in ("long_name", "short_name"):
        if owner.get(k) is not None and not (isinstance(owner[k], str) and owner[k].strip()):
            raise ConfigError(f"owner {k} must be a non-empty string")
    if owner.get("short_name") and len(owner["short_name"].encode()) > 4:
        raise ConfigError("an owner short name is at most 4 bytes")
    for k, v in (desired.get("device") or {}).items():
        if k not in DEVICE_FIELDS:
            raise ConfigError(f"meshtastic_config does not manage device.{k}")
        if v not in DEVICE_FIELDS[k].values():
            raise ConfigError(f"device.{k}: {v!r} is not one of "
                              f"{', '.join(DEVICE_FIELDS[k].values())}")
    for k, v in (desired.get("lora") or {}).items():
        if k not in LORA_FIELDS:
            raise ConfigError(f"meshtastic_config does not manage lora.{k}")
        if k == "hop_limit" and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 7):
            raise ConfigError(f"lora.hop_limit is an integer from 1 to 7, got {v!r}")
    check_security(desired.get("security"))
    seen, primaries = set(), 0
    for ch in desired.get("channels") or []:
        extra = set(ch) - CHANNEL_KEYS
        if extra:
            raise ConfigError(f"channel: unknown keys {sorted(extra)}")
        i = ch.get("index")
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < MAX_CHANNELS:
            raise ConfigError(f"a channel index is 0..{MAX_CHANNELS - 1}, got {i!r}")
        if i in seen:
            raise ConfigError(f"channel {i} is declared twice")
        seen.add(i)
        if ch.get("role") not in CHANNEL_ROLES:
            raise ConfigError(f"channel {i}: role must be one of {', '.join(sorted(CHANNEL_ROLES))}")
        primaries += ch["role"] == "PRIMARY"
        if ch["role"] != "DISABLED":
            if len((ch.get("name") or "").encode()) > 11:
                raise ConfigError(f"channel {i}: a name is at most 11 bytes")
            key_bytes(ch.get("psk", "default"))
    if primaries > 1:
        raise ConfigError("more than one PRIMARY channel is declared")


def _channel_view(ch):
    """A declared channel in the terms it is compared in."""
    if ch["role"] == "DISABLED":
        return {"role": "DISABLED"}
    v = {"role": ch["role"], "name": ch.get("name") or "", "psk": key_view(ch.get("psk", "default"))}
    for k in ("uplink_enabled", "downlink_enabled"):
        if k in ch:
            v[k] = bool(ch[k])
    return v


def plan(desired, facts):
    """What must be written. Returns {"owner": {...}, "device": {...}, "channels": [declared
    channel, ...]} holding only what differs, and a {"before": ..., "after": ...} diff that
    shows keys as fingerprints."""
    check(desired)
    changes, before, after = {}, {}, {}

    owner = {k: v for k, v in (desired.get("owner") or {}).items() if v is not None}
    actual_owner = facts.get("owner") or {}
    if any(actual_owner.get(k) != v for k, v in owner.items()):
        changes["owner"] = {**actual_owner, **owner}
        before["owner"], after["owner"] = dict(actual_owner), changes["owner"]

    device = desired.get("device") or {}
    actual_device = facts.get("device") or {}
    dd = {k: v for k, v in device.items() if actual_device.get(k) != v}
    if dd:
        changes["device"] = dd
        before["device"] = {k: actual_device.get(k) for k in dd}
        after["device"] = dd

    lora = {k: v for k, v in (desired.get("lora") or {}).items() if v is not None}
    actual_lora = facts.get("lora_api") or {}
    ld = {k: v for k, v in lora.items() if actual_lora.get(k) != v}
    if ld:
        changes["lora"] = ld
        before["lora"] = {k: actual_lora.get(k) for k in ld}
        after["lora"] = ld

    actual_ch = {c["index"]: c for c in facts.get("channels") or []}
    for ch in sorted(desired.get("channels") or [], key=lambda c: c["index"]):
        want = _channel_view(ch)
        a = actual_ch.get(ch["index"])          # the state line lists enabled slots only
        have = {"role": "DISABLED"} if a is None else {
            "role": a["role"], "name": a["name"], "psk": _actual_key_view(a),
            **{k: a[k] for k in ("uplink_enabled", "downlink_enabled") if k in want}}
        if have != want:
            changes.setdefault("channels", []).append(ch)
            before.setdefault("channels", {})[ch["index"]] = have
            after.setdefault("channels", {})[ch["index"]] = want

    # Admin keys are a set: the node obeys each alike, so their order is not a difference.
    sec = {k: v for k, v in (desired.get("security") or {}).items() if v is not None}
    actual_sec = facts.get("security_api") or {}
    sd = {}
    if "admin_key" in sec:
        want_keys = [admin_key_view(admin_key_bytes(k)) for k in sec["admin_key"]]
        if sorted(want_keys) != sorted(actual_sec.get("admin_key") or []):
            sd["admin_key"] = want_keys
    if "is_managed" in sec and actual_sec.get("is_managed") != sec["is_managed"]:
        sd["is_managed"] = sec["is_managed"]
    if sd:
        changes["security"] = sd
        before["security"] = {k: actual_sec.get(k) for k in sd}
        after["security"] = dict(sd)
    return changes, {"before": before, "after": after}


def managed_refusal(changes, facts):
    """Why these changes cannot be written, or None: a managed node drops every local write."""
    if not changes or not facts.get("managed"):
        return None
    return ("the node is managed (security.is_managed): it takes configuration only from a "
            f"remote admin, so the bench cannot change its {', '.join(sorted(changes))}. "
            "Change it with a node whose key is among its admin keys, or erase its config region")


def phases(changes):
    """The changes in the order they are written, each read back before the next: everything
    else first, then the security section by itself. A node that becomes managed takes nothing
    after that, and a transaction that makes it managed loses its own commit."""
    rest = {k: v for k, v in changes.items() if k != "security"}
    return [c for c in (rest, {k: v for k, v in changes.items() if k == "security"}) if c]


def security_api(sec):
    """The security fields compared here, from a node's SecurityConfig as the phone API gives it."""
    return {"admin_key": [admin_key_view(k) for k in sec.admin_key],
            "is_managed": bool(sec.is_managed)}


def write_security(node, change):
    """Write the security section alone, outside any transaction. What the node sent is sent
    back with only the declared fields changed, so its identity key pair stays as it is."""
    sec = node.localConfig.security
    if "admin_key" in change:
        del sec.admin_key[:]
        sec.admin_key.extend(admin_key_bytes(k) for k in change["admin_key"])
    if "is_managed" in change:
        sec.is_managed = bool(change["is_managed"])
    node.writeConfig("security")


#: Seconds between the admin messages of one write. The stock library sends them back to back
#: and does not wait for the node; stock firmware queues what the phone API sends in a small
#: queue and, when it is full, drops the oldest ("fromRadioQ full, drop oldest!"). Seen on
#: meshtasticd 2.7.26 on a fresh node: an edit transaction's begin and its owner write were
#: dropped, so the rest was saved outside the transaction and the owner never came.
PACE = 0.5


def write(iface, changes, sleep=time.sleep):
    """Send one phase of the planned changes through a connected stock-library interface:
    owner, device, LoRa, display (netconfig's) and channels as one edit transaction, so the
    node saves (and, if it must, restarts) once; the security section by itself."""
    from meshtastic.protobuf import channel_pb2, config_pb2  # pylint: disable=import-outside-toplevel

    node = iface.localNode
    if "security" in changes:
        if set(changes) != {"security"}:
            raise ConfigError("the security section is written by itself (config.phases)")
        write_security(node, changes["security"])
        return

    def paced(call, *args):
        call(*args)
        sleep(PACE)

    paced(node.beginSettingsTransaction)
    if "owner" in changes:
        paced(node.setOwner, changes["owner"].get("long_name"), changes["owner"].get("short_name"))
    if "device" in changes:
        dev = node.localConfig.device
        if "role" in changes["device"]:
            dev.role = config_pb2.Config.DeviceConfig.Role.Value(changes["device"]["role"])
        if "rebroadcast_mode" in changes["device"]:
            dev.rebroadcast_mode = config_pb2.Config.DeviceConfig.RebroadcastMode.Value(
                changes["device"]["rebroadcast_mode"])
        paced(node.writeConfig, "device")
    if "lora" in changes:
        cfg = node.localConfig.lora
        for k, v in changes["lora"].items():
            setattr(cfg, k, v)
        paced(node.writeConfig, "lora")
    if "display" in changes:
        cfg = node.localConfig.display
        for k, v in changes["display"].items():
            setattr(cfg, k, config_pb2.Config.DisplayConfig.DisplayMode.Value(v)
                    if k == "displaymode" else v)
        paced(node.writeConfig, "display")
    for want in changes.get("channels", []):
        ch = node.channels[want["index"]]
        ch.role = channel_pb2.Channel.Role.Value(want["role"])
        if want["role"] == "DISABLED":
            ch.settings.Clear()
        else:
            ch.settings.name = want.get("name") or ""
            ch.settings.psk = key_bytes(want.get("psk", "default"))
            for k in ("uplink_enabled", "downlink_enabled"):
                if k in want:
                    setattr(ch.settings, k, bool(want[k]))
        paced(node.writeChannel, want["index"])
    node.commitSettingsTransaction()
