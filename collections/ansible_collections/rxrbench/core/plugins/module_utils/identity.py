"""What an image is and what a device runs, in one shape, so "needs flashing" is a comparison.

    {"family": ..., "target": ..., "version": ..., "hash": ..., "build": ..., "features": ...}

- `family`   a row of the families table (zephyr_mcumgr, bare, a plugin's, ...)
- `target`   what the image is for: a Zephyr board name, or the string an update system calls a device and its images compatible by
- `version`  the version the image carries (an MCUboot header, a bundle's manifest)
- `hash`     the image's own hash: MCUboot's SHA-256 TLV (what MCUmgr reports for a slot),
             or an update bundle's hash (what the device records for the slot it filled)
- `build`    a build id, where the device reports one (the firmware's git commit)
- `features` the recipe features built into the image (the family's `features`), sorted. Two
             images of one build may differ by these alone
- a family's own key: the field it tells its images apart by beside build and version (a
             class number, a role name). Which fields these are comes from the loaded
             families (`register`); none is named here

A field a source cannot give is None. Sources differ: a board read over its shell gives a
build id but no hash, the same board read over MCUmgr gives the hash but no build id.

`same()` is strict about what it does not know. Two identities are the same when a STRONG
field (hash or build) is known on both sides and equal, and nothing known on both sides
differs. A version alone never proves it: a version number is reused across builds.
Checked on hardware (docs/HARDWARE.md, "Bluetooth LE and MCUmgr"): a slot-0 hash over
serial MCUmgr equals the TLV hash of the image file.
"""

BASE = ("family", "target", "version", "hash", "build", "features")
STRONG = ("hash", "build")

# {family: the field its images are told apart by beside build and version}, as the loaded
# families say (familyspec.load): a field of that name is an identity field too. This module
# names none itself.
_KEYS = {}


def register(keys):
    """Take the loaded families' identity keys ({family: key or None})."""
    _KEYS.clear()
    _KEYS.update({f: k for f, k in (keys or {}).items() if k})


def fields():
    """Every identity field: the ones any device has, then the loaded families' own."""
    return BASE + tuple(sorted(set(_KEYS.values())))


def make(family, **kw):
    unknown = set(kw) - set(fields())
    if unknown:
        raise ValueError(f"not identity fields: {sorted(unknown)}")
    ident = {f: None for f in fields()}
    ident.update(family=family, **kw)
    return ident


def same(a, b):
    """True, False, or None when the two cannot be compared (no strong field in common)."""
    both = [f for f in fields() if a.get(f) is not None and b.get(f) is not None]
    if any(a[f] != b[f] for f in both):
        return False
    return True if any(f in STRONG for f in both) else None


def differences(a, b):
    """{field: (a, b)} for the fields known on both sides that differ."""
    return {f: (a[f], b[f]) for f in fields()
            if a.get(f) is not None and b.get(f) is not None and a[f] != b[f]}


def strip_build(version):
    """MCUboot prints 0.4.39+0; MCUmgr reports 0.4.39 when the build number is 0."""
    return version[:-2] if isinstance(version, str) and version.endswith("+0") else version


def smp_version(version):
    """A version as MCUmgr reports it, in MCUboot's spelling: Zephyr's image manager prints
    major.minor.revision and the build counter after a fourth dot when it is not 0
    (`0.4.46.45`), where MCUboot and a manifest say 0.4.46+45."""
    if isinstance(version, str) and "+" not in version:
        parts = version.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            v, n = ".".join(parts[:3]), int(parts[3])
            return f"{v}+{n}" if n else v
    return strip_build(version)


def of_smp(images, family="zephyr_mcumgr"):
    """A device, from MCUmgr's image state: the slot that is running. Returns the identity
    and the slot's flags (confirmed, pending): an image that runs but is not confirmed is
    on trial, and a reset takes the device back to the other slot."""
    active = [i for i in images if i.get("active")]
    if len(active) != 1:
        raise ValueError(f"{len(active)} active image slots; cannot tell what is running")
    i = active[0]
    return (make(family, version=smp_version(i.get("version")), hash=i.get("hash")),
            {"slot": i.get("slot"), "confirmed": bool(i.get("confirmed")),
             "pending": bool(i.get("pending"))})


def mcuboot_version(info):
    """An MCUboot image's version as a board running it says it, from imginfo's fields:
    imginfo keeps the header's build counter apart (`version` 0.4.46, `build` 45), a board
    reports 0.4.46+45, and 0.4.46 when the counter is 0."""
    v, n = info.get("version"), info.get("build")
    if isinstance(v, str) and "+" not in v and isinstance(n, int) and n:
        return f"{v}+{n}"
    return strip_build(v)


def of_mcuboot_image(info, family):
    """An MCUboot image file, from its header and TLVs (imginfo's fields)."""
    key = _KEYS.get(family)
    return make(family, version=mcuboot_version(info), hash=info.get("sha256"),
                **({key: info.get(key)} if key else {}))
