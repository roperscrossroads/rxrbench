"""The MeshCore vocabulary: reading a node that speaks MeshCore's CLI (zephcore, stock MeshCore).

A zephcore node prints its state as one line, `zephcore state` -> `~S{json}*crc16` (the framing
module_utils/state.py reads for every firmware). Stock MeshCore has no state line: its facts are
put together from single CLI answers (`ver`, `get name`, `get radio`, ...), each `> <value>`,
and say `state_supported: False`.

Both give one shape:

    {"state_supported", "family", "version", "build", "board", "board_tag", "role", "name",
     "pubkey_fp", "pw_fp", "radio": {freq_mhz, bw_khz, sf, cr, tx_dbm, tx_enabled},
     "storage": {fs, epoch, used, total}}

A field a source cannot give is None. No secret is ever in it: the admin password only as a
fingerprint (`pw_fp`, crc32 of "zephcore/admin-password:" + password, as the firmware prints it).
"""
import binascii
import re

import time

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, familyspec, identity
from . import state

STATE_COMMAND = "zephcore state"
STATE_VERSION = 1
# What a node without a state line is asked, in order (stock MeshCore).
LEGACY_COMMANDS = ("ver", "get name", "get radio", "get tx", "get role", "get public.key")
PW_LABEL = b"zephcore/admin-password:"
# get radio prints its frequency as a float (910.5250244): compare to this many MHz.
FREQ_TOLERANCE_MHZ = 0.0005


class MeshCoreError(ValueError):
    pass


def pw_fp(password):
    """The fingerprint the firmware prints for an admin password."""
    return f"{binascii.crc32(PW_LABEL + password.encode()) & 0xffffffff:08x}"


def key_fp(pubkey_hex):
    return f"{binascii.crc32(bytes.fromhex(pubkey_hex)) & 0xffffffff:08x}" if pubkey_hex else None


def value(reply):
    """A `get` answer, "> value", to the value; anything else (an error) raises."""
    reply = (reply or "").strip()
    if not reply.startswith(">"):
        raise MeshCoreError(f"not a value: {reply!r}")
    return reply[1:].strip()


def parse_radio(text):
    """"910.5250244,62.5,7,5" -> {freq_mhz, bw_khz, sf, cr}."""
    try:
        f, bw, sf, cr = [p.strip() for p in text.split(",")]
        return {"freq_mhz": round(float(f), 4), "bw_khz": float(bw), "sf": int(sf), "cr": int(cr)}
    except ValueError as e:
        raise MeshCoreError(f"not a radio setting: {text!r}") from e


def state_facts(output):
    """The facts in a `zephcore state` answer, or None when the node printed no state line."""
    j = state.parse_line(output, unknown=("*float*",))
    if j is None:
        return None
    if j.get("v") != STATE_VERSION:
        raise state.StateError(f"state version {j.get('v')!r}, this tool reads {STATE_VERSION}")
    r = j.get("radio") or {}
    return {
        "state_supported": True,
        "family": j.get("family"), "version": j.get("version"), "build": j.get("build"),
        "board": j.get("board"), "board_tag": j.get("board_tag"), "role": j.get("role"),
        "name": j.get("name"), "pubkey_fp": j.get("pubkey_fp"), "pw_fp": j.get("pw_fp"),
        "radio": {"freq_mhz": round(float(r["freq_mhz"]), 4)
                  if r.get("freq_mhz") is not None else None,
                  "bw_khz": r.get("bw_khz"), "sf": r.get("sf"), "cr": r.get("cr"),
                  "tx_dbm": r.get("tx_dbm"), "tx_enabled": r.get("tx_enabled")},
        "storage": dict(j.get("storage") or {}),
        "uptime_s": j.get("uptime_s"),
    }


def legacy_facts(outputs, family="meshcore"):
    """Facts from single CLI answers ({command: answer} for LEGACY_COMMANDS)."""
    def get(cmd):
        try:
            return value(outputs.get(cmd))
        except MeshCoreError:
            return None
    radio = parse_radio(get("get radio")) if get("get radio") else {}
    tx = get("get tx")
    ver = (outputs.get("ver") or "").strip()
    # a release says "v1.17.1-d929643 (Build: 14-Aug-2026)": its version, the commit it is from
    rel = re.match(r"(v\d[\w.]*)-([0-9a-f]{7,40})\b", ver)
    return {
        "state_supported": False, "family": family,
        "version": rel.group(1) if rel else (ver.split()[0] if ver else None),
        "build": rel.group(2) if rel else None,
        "board": None, "board_tag": None, "role": get("get role"), "name": get("get name"),
        "pubkey_fp": key_fp(get("get public.key")), "pw_fp": None,
        "radio": {**{"freq_mhz": None, "bw_khz": None, "sf": None, "cr": None}, **radio,
                  "tx_dbm": int(tx) if tx and tx.lstrip("-").isdigit() else None,
                  "tx_enabled": None},
        "storage": {}, "uptime_s": None,
    }


def of_facts(facts, family):
    """The identity of a MeshCore-vocabulary board, from its facts."""
    return identity.make(family, target=facts.get("board"), version=facts.get("version"),
                         build=facts.get("build"), role=facts.get("role"))


def read(board, family, command=STATE_COMMAND, deadline=10.0):
    """A board's facts through its broker: its state line, or (no state line) the single
    answers. `board` is a broker_client.Board."""
    out = board.exec(command, deadline=deadline, dialect="meshcore") if command else ""
    facts = state_facts(out) if command else None
    if facts is None:
        facts = legacy_facts({c: board.exec(c, deadline=deadline, dialect="meshcore")
                              for c in LEGACY_COMMANDS}, family)
    elif any(facts["radio"].get(k) is None for k in RADIO_KEYS):
        # A state line that could not print the radio's numbers (*float* on zephcore's
        # hardware builds): MeshCore's own `get radio` formats them.
        facts["radio"].update(parse_radio(value(board.exec("get radio", deadline=deadline,
                                                           dialect="meshcore"))))
    return facts


# ---- writing: pure plans, in the safety order the Meshtastic vocabulary uses --------------

# Named radio presets a layout may use instead of the four numbers. MeshCore's published
# "USA" preset is the default the firmware boots with.
PRESETS = {"usa": {"freq_mhz": 910.525, "bw_khz": 62.5, "sf": 7, "cr": 5}}


def declared_radio(block):
    """A layout's `meshcore.radio`, with a named `preset` expanded (explicit keys win)."""
    block = dict(block or {})
    preset = block.pop("preset", None)
    if preset is None:
        return block
    if str(preset).lower() not in PRESETS:
        raise MeshCoreError(f"unknown MeshCore preset {preset!r} (known: {sorted(PRESETS)})")
    return {**PRESETS[str(preset).lower()], **block}


REBOOT = "reboot"           # a plan step: reboot and wait for the board (radio changes need it)
RADIO_KEYS = ("freq_mhz", "bw_khz", "sf", "cr")
# MeshCore's own ranges (CommonCLI): an advert interval in minutes is 0 (off) or 60-240; a flood
# advert interval in hours is 0 or 3-168.
ADVERT_MIN = (60, 240)
FLOOD_H = (3, 168)


def _fmt(x):
    return f"{x:g}" if isinstance(x, float) else str(x)


def radio_differs(desired, actual):
    """Which of the radio's parameters (frequency, bandwidth, SF, CR) differ."""
    out = []
    for k in RADIO_KEYS:
        if desired.get(k) is None:
            continue
        a = actual.get(k)
        if k == "freq_mhz":
            if a is None or abs(float(desired[k]) - float(a)) > FREQ_TOLERANCE_MHZ:
                out.append(k)
        elif a is None or float(desired[k]) != float(a):
            out.append(k)
    return out


def radio_plan(desired, facts):
    """The console commands that bring the radio to `desired` ({freq_mhz, bw_khz, sf, cr,
    tx_dbm, tx_enabled}), in order: the transmitter goes off before power or parameters change;
    a parameter change and the reboot that applies it come next, the power after them (set again
    after such a reboot), and the transmitter goes on only at the end, when declared."""
    actual = facts.get("radio") or {}
    params = radio_differs(desired, actual)
    power = desired.get("tx_dbm") is not None and desired["tx_dbm"] != actual.get("tx_dbm")
    want_tx = desired.get("tx_enabled")
    if actual.get("tx_enabled") is None:
        # a node with no transmitter switch (the stock CLI: it transmits whenever it is powered):
        # nothing to turn on or off, the declaration's `tx_enabled` is for the RF rules only
        want_tx = None
    tx_on_now = bool(actual.get("tx_enabled"))
    plan = []
    if tx_on_now and (params or power or want_tx is False):
        plan.append("zephcore tx off")
        tx_on_now = False
    # The parameters (and the reboot that applies them) before the power: a reboot must not
    # come after a setting it could lose (zephcore did not keep TX power across one).
    if params:
        merged = {k: desired.get(k, actual.get(k)) for k in RADIO_KEYS}
        plan.append("set radio " + ",".join(_fmt(merged[k]) for k in RADIO_KEYS))
        plan.append(REBOOT)
    if power or (params and desired.get("tx_dbm") is not None):
        plan.append(f"set tx {int(desired['tx_dbm'])}")
    if want_tx and not tx_on_now:
        plan.append("zephcore tx on")
    return plan


# Prefs a layout declares under `meshcore.prefs` are any of the CLI's `get`/`set` keys but
# these: the ones other parts of the block own, and the secrets.
PREFS_OWNED = {"name", "radio", "freq", "tx", "advert.interval", "flood.advert.interval",
               "prv.key", "guest.password", "password", "public.key", "role"}
PREF_KEY = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
# What MeshCore's `set name` refuses (CommonCLI isValidName), and the bytes its name holds.
NAME_REFUSED = set("[]\\:,?*")
NAME_MAX = 31


def pref_text(v):
    """A declared value as the CLI writes it: on/off for a boolean, else its text."""
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v)


def pref_same(declared, answer):
    """A declared pref against the node's `get` answer: numbers compared as numbers (the
    firmware prints a float with its own digits: 12.3456789 for 12.345679), else as text."""
    want, got = pref_text(declared).strip(), str(answer if answer is not None else "").strip()
    if want == got:
        return True
    try:
        a, b = float(want), float(got)
    except ValueError:
        return False
    return abs(a - b) <= max(1e-6, 1e-6 * abs(a))


def config_plan(desired, facts, gets, restored=False):
    """The console commands for name, advert intervals, declared prefs and the admin password.
    `gets` holds the answers to `get <key>` for the advert intervals and each declared pref (the
    state line does not carry them). The password is compared by fingerprint and written by
    `password <pw>`: the step is returned as (command, redacted) pairs, so a caller never has to
    print the secret to say what it does. A node that prints no fingerprint (stock MeshCore)
    cannot be compared: its password is written when anything else here is, or when the node
    was just `restored` (its key given back, its radio or storage rewritten: an erase), and a
    node found as declared is left alone."""
    plan = []
    if desired.get("name") is not None and desired["name"] != facts.get("name"):
        plan.append((f"set name {desired['name']}", f"set name {desired['name']}"))
    adv = desired.get("advert") or {}
    if adv.get("local_interval_min") is not None and \
            str(adv["local_interval_min"]) != str(gets.get("advert.interval")):
        plan.append((f"set advert.interval {int(adv['local_interval_min'])}",) * 2)
    if adv.get("flood_interval_h") is not None and \
            str(adv["flood_interval_h"]) != str(gets.get("flood.advert.interval")):
        plan.append((f"set flood.advert.interval {int(adv['flood_interval_h'])}",) * 2)
    for key, val in sorted((desired.get("prefs") or {}).items()):
        if not pref_same(val, gets.get(key)):
            plan.append((f"set {key} {pref_text(val)}",) * 2)
    pw = desired.get("admin_password")
    if pw is not None:
        if facts.get("state_supported") is False:
            if plan or restored:
                plan.append((f"password {pw}", "password <secret> (unreadable here: written "
                                               "with the node's other prefs)"))
        elif pw_fp(pw) != facts.get("pw_fp"):
            plan.append((f"password {pw}", "password <secret>"))
    return plan


def check(name, block):
    """Problems with a layout's `meshcore:` block that the firmware would refuse (or truncate)."""
    out = []
    n = block.get("name")
    if n is not None and (not str(n).strip() or str(n) != str(n).strip()
                          or NAME_REFUSED & set(str(n)) or len(str(n).encode()) > NAME_MAX):
        out.append(f"{name}: meshcore.name {n!r}: MeshCore takes 1 to {NAME_MAX} bytes, without "
                   f"leading or trailing spaces or any of {''.join(sorted(NAME_REFUSED))}")
    for key in (block.get("prefs") or {}):
        if key in PREFS_OWNED or not PREF_KEY.match(str(key)):
            out.append(f"{name}: meshcore.prefs.{key}: not a pref this block takes (a CLI key; "
                       "name, radio, tx, adverts and secrets have their own places)")
    adv = block.get("advert") or {}
    a = adv.get("local_interval_min")
    if a is not None and a != 0 and not ADVERT_MIN[0] <= int(a) <= ADVERT_MIN[1]:
        out.append(f"{name}: meshcore.advert.local_interval_min {a} is not 0 or "
                   f"{ADVERT_MIN[0]}-{ADVERT_MIN[1]} (MeshCore refuses it)")
    f = adv.get("flood_interval_h")
    if f is not None and f != 0 and not FLOOD_H[0] <= int(f) <= FLOOD_H[1]:
        out.append(f"{name}: meshcore.advert.flood_interval_h {f} is not 0 or "
                   f"{FLOOD_H[0]}-{FLOOD_H[1]}")
    pw = block.get("admin_password")
    # A layout names its password from its secrets ("{{ ... }}"): only the value can be
    # measured, and meshcore_config checks it once Ansible has filled it in.
    if pw is not None and "{{" not in str(pw) and not 9 <= len(str(pw)) <= 15:
        out.append(f"{name}: meshcore.admin_password must be 9 to 15 characters (MeshCore keeps "
                   f"16 bytes and truncates silently)")
    return out


def booted(family, said):
    """True when `said` (what the board printed under a reboot command) holds the boot banner
    its family names (familyspec `reboot.banner`)."""
    banner = (familyspec.of(family).get("reboot") or {}).get("banner")
    return bool(banner and said and re.search(banner, said, re.M))


def await_reboot(board, family, command, before_uptime, settle=60.0, deadline=10.0,
                 before_at=None, before_opens=None, said=None):
    """Wait until the node has really rebooted and answers again. An answer alone does not
    prove it: zephcore reboots about 0.5 s after answering a wipe, and a read inside that window
    reaches the old process. So: the console must go down first (a sim exits; a USB board may
    re-enumerate: a read fails, or the broker's count of port opens has gone up past
    `before_opens`, taken before the command, since a command finds a closed port reopened for
    it), or its uptime must be less than it would be had the node kept running: the
    uptime read at `before_at` (time.monotonic()) plus the time since. Measured, not "less than
    before": a Heltec's console stays open across its software reboot, and a node wiped a few
    seconds earlier boots back to an uptime above the 4 s it had. Or `said`, what the board
    printed under the command, holds its family's boot banner: stock MeshCore on an ESP32 has no
    uptime and its console never goes down."""
    import time  # pylint: disable=import-outside-toplevel
    before_at = time.monotonic() if before_at is None else before_at
    end = time.monotonic() + settle
    went_down = booted(family, said)
    while time.monotonic() < end:
        try:
            f = read(board, family, command, deadline)
        except Exception:  # noqa: BLE001 - the console is down while it reboots
            went_down = True
            time.sleep(0.5)
            continue
        up = (f or {}).get("uptime_s")
        kept_running = None if before_uptime is None else \
            before_uptime + (time.monotonic() - before_at)
        # uptime_s is whole seconds: a margin of 2 s keeps a running node from looking rebooted
        went_down = went_down or broker_client.reopened(board, before_opens)
        if f is not None and (went_down or (up is not None and kept_running is not None
                                            and up + 2 < kept_running)):
            return f
        time.sleep(0.3)
    raise MeshCoreError(f"the board did not reboot and come back within {settle:.0f} s")


def run_steps(board, steps, family, command=STATE_COMMAND, settle=60.0, deadline=10.0):
    """Send plan steps (a command, REBOOT, or a (command, redacted) pair) through the broker
    in order. A command must be answered OK (or `tx on|off`); REBOOT sends `reboot` and waits
    until the board answers again (the broker reopens the console when it comes back). Returns
    the redacted transcript. Raises MeshCoreError on a refused step."""
    done = []
    for step in steps:
        cmd, shown = step if isinstance(step, tuple) else (step, step)
        if cmd == REBOOT:
            before, before_at = read(board, family, command, deadline), time.monotonic()
            opened = broker_client.opens(board)
            said = board.exec("reboot", deadline=3.0, dialect="meshcore", expect_reply=False)
            await_reboot(board, family, command, (before or {}).get("uptime_s"), settle, deadline,
                         before_at, opened, said)
            done.append("reboot")
            continue
        reply = board.exec(cmd, deadline=deadline, dialect="meshcore")
        # stock MeshCore answers `password <pw>` with "password now: <pw>" (the broker shows the
        # secret as <secret>, in its log and in this answer)
        if not (reply.startswith("OK") or reply.startswith("tx ")
                or (cmd.startswith("password ") and reply.startswith("password now: "))):
            raise MeshCoreError(f"`{shown}` was refused: {reply}")
        done.append(f"{shown} -> {reply}")
    return done


def storage_plan(desired, facts):
    """None when the node's storage epoch is the declared one; else the wipe that makes it so,
    `zephcore wipe <epoch> keep|new` (keep: everything but the identity goes; new: a new key).
    A wipe is a state: bumping the declared epoch is how a layout says "wipe these once"."""
    epoch = desired.get("epoch")
    if epoch is None:
        return None
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise MeshCoreError(f"a storage epoch is an integer >= 1, got {epoch!r}")
    if (facts.get("storage") or {}).get("epoch") == epoch:
        return None
    return f"zephcore wipe {epoch} {'keep' if desired.get('keep_identity', True) else 'new'}"


def wipe(board, step, family, command=STATE_COMMAND, settle=60.0):
    """Run a wipe and wait for the node to have rebooted; returns its facts afterwards."""
    before, before_at = read(board, family, command), time.monotonic()
    opened = broker_client.opens(board)
    said = board.exec(step, deadline=3.0, dialect="meshcore", expect_reply=False)
    return await_reboot(board, family, command, (before or {}).get("uptime_s"), settle,
                        before_at=before_at, before_opens=opened, said=said)


# ---- the identity key (escrow) -------------------------------------------------------
# zephcore: `zephcore key export confirm` answers one bare line ~K<base64 of the 64-byte private
# key>*<crc16>, CRC-16/XMODEM over the base64 text; `set prv.key <128 hex>` (never echoed) takes
# the key at the next reboot. The broker keeps ~K payloads out of its log.
KEY_EXPORT = "zephcore key export confirm"
KEY_LEN = 64


def parse_key(line):
    """The private key from a ~K line; MeshCoreError if it is not one or is damaged."""
    import base64  # pylint: disable=import-outside-toplevel
    import binascii  # pylint: disable=import-outside-toplevel
    line = line.strip()
    if not line.startswith("~K") or "*" not in line:
        raise MeshCoreError("the key export did not answer with a ~K line")
    b64, crc = line[2:].rsplit("*", 1)
    if f"{binascii.crc_hqx(b64.encode(), 0):04x}" != crc.lower():
        raise MeshCoreError("the key export's checksum does not match")
    key = base64.b64decode(b64, validate=True)
    if len(key) != KEY_LEN:
        raise MeshCoreError(f"the exported key is {len(key)} bytes, not {KEY_LEN}")
    return key


# Stock MeshCore (no state line): `get prv.key`, answered only on the serial console, prints
# "> <128 hex>"; the broker's meshcore dialect keeps that answer out of its log.
STOCK_KEY_GET = "get prv.key"


def parse_hex_key(reply):
    """The private key from a `get prv.key` answer; MeshCoreError (never quoting the answer,
    which may be most of a key) if it is not one."""
    try:
        key = bytes.fromhex(value(reply))
    except ValueError:
        raise MeshCoreError("`get prv.key` did not answer with a key") from None
    if len(key) != KEY_LEN:
        raise MeshCoreError(f"the key `get prv.key` gave is {len(key)} bytes, not {KEY_LEN}")
    return key


def key_export(board, command=STATE_COMMAND, deadline=10.0):
    """The node's private key: zephcore's ~K line, or, on a node without a state line (stock
    MeshCore, `command` None), its answer to `get prv.key`."""
    if command is None:
        return parse_hex_key(board.exec(STOCK_KEY_GET, deadline=deadline, dialect="meshcore"))
    return parse_key(board.exec(KEY_EXPORT, deadline=deadline, dialect="meshcore"))


def key_import(board, family, key, command=STATE_COMMAND, settle=60.0, deadline=10.0):
    """Give the node `key` (64 bytes) and reboot it; returns its facts after the reboot."""
    if len(key) != KEY_LEN:
        raise MeshCoreError(f"a key is {KEY_LEN} bytes")
    before, before_at = read(board, family, command, deadline), time.monotonic()
    opened = broker_client.opens(board)
    reply = board.exec(f"set prv.key {key.hex()}", deadline=deadline, dialect="meshcore")
    if not reply.startswith("OK"):
        raise MeshCoreError(f"set prv.key: {reply}")
    said = board.exec("reboot", deadline=min(deadline, 3.0), dialect="meshcore",
                      expect_reply=False)
    return await_reboot(board, family, command, (before or {}).get("uptime_s"), settle, deadline,
                        before_at, opened, said)

