"""What a module on a bench host knows of device families: data it was handed.

A module never imports a family's code (a plugin collection's code is not shipped with a
core module). The inventory resolves every loaded family to a spec and publishes the table
as `bench_families`; a module takes it as its `families` argument and calls `load()`.

    console        the kind of console (how a command is asked and answered), or None
    identity_key   the field its images are told apart by beside build and version, or None
    identity       how its state line is read into an identity, or None when it prints none:
                     command, dialect, null_tokens   how the line is asked for
                     version                          the `v` the line must carry, or None
                     names_family                     the line's `family` must be this family
                     fields {identity field: key}     where the line says each field
                     features [..]                    the features an identity lists (others in
                                                      the line are not what tells images apart)
                     strip_build                      `1.2.3+0` is `1.2.3`
                     fallback {command, require, fields {field: regex}, optional, ints}
                                                      firmware from before the state line
                   or, for firmware with no state line that answers on its console in
                   prose: command, dialect, lineless (true), and the fallback, which alone
                   says the identity; also [{command, field, pattern}]: what the survey asks
                   once it has answered (a role)
                   or, for firmware with no state line, a probe its plugin deploys:
                     tool                             the script (in ~/rxrbench/bin), run with
                                                      the board's guarded port, one JSON object out
                     fields {identity field: key}     where the probe says each field
                     build_from {key, pattern}        a build id inside another value
                     timeout                          seconds before the probe is killed
    image          match [..]   manifest fields that must equal the board's declaration
                   bytes_check  {field, args}: the field read from the image's own bytes
    regions        [[name, flash map key], ..] of its config regions, the first one essential
    reboot         {reboot, dialect, expect_reply, uptime, banner}; uptime is {command, pattern,
                   scale} or {state_key}, or None when the board cannot say how long it has been
                   up; banner (optional) a regex of what the board prints as it boots, which
                   proves a reboot on a console that stays open across it; ready (optional,
                   for a board with no uptime) a command it answers once it is back
    reset_command  the command that says why the board last reset, or None
    bonds_command  the command that lists the Bluetooth bonds it holds, or None
    bonds_prefix   what starts a bond's line in that list, before the host's address
    uf2_enter      what asks a running app for its bootloader's UF2 mode: {shell_command (this
                   family's, on its shell), shell_usb [the USB vendor ids (or vid:pid) its shell
                   runs on; Zephyr's 2fe3 when not given], app_tools [command lines, `{port}` the app's
                   port] for the other firmware its boards arrive with, bootloader (a word
                   the UF2 bootloader's version must carry before a first flash)}, or None.
                   A board read as running the family is asked only this way; on a first
                   flash every family's is offered (docs/ARCHITECTURE.md#entering-uf2-mode)
    survey         how the survey recognises the firmware on a USB device, or None:
                     kinds [..]        the kinds of USB device asked for its state line
                     keep, flags       {probe key: state key}: what is kept of the line, and
                                       keys whose presence alone is noted
                     tool              a probe script the plugin deploys, for a firmware with no
                                       state line; run when the device says it runs it (`modes`)
                                       or answered as nothing else (`unanswered` kinds)
                     probes [..]       the kinds of debug probe (`swd-probe`) the tool reads
                                       through: run with `--probe <registry name> --wired
                                       <json>` (the chips wired to it) instead of a port
"""
import re

from . import identity, stateline

GENERIC = {"console": None, "identity_key": None, "identity": None, "image": {"match": [], "bytes_check": None},
           "regions": None, "reboot": None, "reset_command": None, "bonds_command": None, "bonds_prefix": None,
           "uf2_enter": None, "survey": None}

_TABLE = {}

#: A module's argument for the table.
ARGUMENT = {"type": "dict", "default": {}}


def load(table):
    """Take the table for this run (a module's `families` argument)."""
    _TABLE.clear()
    _TABLE.update(table or {})
    identity.register({f: (s or {}).get("identity_key") for f, s in _TABLE.items()})


def of(family):
    """A family's spec; one the table does not hold has no console and no state line."""
    return {**GENERIC, **(_TABLE.get(family) or {})}


def families():
    """Every family in the table."""
    return list(_TABLE)


def unloaded(family):
    """What to add to a refusal about a family the table does not hold: a board may declare
    firmware no plugin manages (it is then left alone), and a refusal says that is why."""
    return "; no loaded plugin brings this family" if family and family not in _TABLE else ""


def state_command(spec):
    """The command that prints a family's state line (`spec` is its familyspec entry), or None
    for a family that prints none: no identity, one read by a probe, or one in prose only
    (`lineless`)."""
    ident = (spec or {}).get("identity") or {}
    return None if ident.get("lineless") or ident.get("tool") else ident.get("command")


def with_identity(exclude=None):
    """[family] of every family whose state line can be read, but `exclude`."""
    return [f for f, s in _TABLE.items() if f != exclude and (s or {}).get("identity")]


def image_refusals(family, manifest, declared):
    """Why an image must not go on a board declared `declared` (its `firmware:` block): a
    field the family tells its images apart by differs."""
    out = []
    for field in of(family)["image"]["match"]:
        have, want = manifest.get(field), declared.get(field)
        if field == "features":
            have, want = sorted(have or []), sorted(want or [])
            if have != want:
                out.append(f"the image has features {have}; the layout declares {want} for this board")
        elif have != want:
            out.append(f"the image is {field} {have!r}; the layout declares {field} {want!r} "
                       f"for this board")
    return out


def exact_features(family):
    """Does an image of this family have exactly the features its manifest lists?"""
    return "features" in of(family)["image"]["match"]


def fields_of_line(spec, j):
    """The identity fields a state line's JSON gives, by a family's `identity` spec."""
    out = {}
    for field, key in (spec.get("fields") or {}).items():
        if field == "features":
            if key in j:
                out[field] = sorted(set(j[key] or []) & set(spec.get("features") or []))
        elif j.get(key) is not None:
            out[field] = j[key]
    if spec.get("strip_build"):
        out["version"] = identity.strip_build(out.get("version"))
    return out


def fields_of_fallback(fallback, output):
    """The identity fields in the answer of a fallback command (prose, read by pattern)."""
    out = {}
    for field, pattern in fallback["fields"].items():
        m = re.search(pattern, output, re.M)
        if not m:
            if field in (fallback.get("optional") or []):
                continue
            raise stateline.StateError(f"cannot read {field} from `{fallback['command']}`")
        out[field] = int(m.group(1)) if field in (fallback.get("ints") or []) else m.group(1)
    return out
