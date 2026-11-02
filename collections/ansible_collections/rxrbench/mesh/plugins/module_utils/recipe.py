"""A firmware build recipe as data, rendered to an argv list (never a shell string).

`catalog/zephyrtastic.yml` says what each fleet class is built from; a board's declared `features`
add overlays. `render()` produces the `west build` argv, and `image_key()` the identity of the
image that argv produces from a given source commit: same key, same image, build once.

Build-system rules this encodes:
- CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION is a Kconfig string: its value must arrive with literal
  quotes, or the build dies with a bare "Aborting due to Kconfig warnings".
- -DZEPHYR_EXTRA_MODULES=<tree> is what makes west compile <tree>. Without it the module
  resolves to the manifest's main/ whatever app path is given, silently.
- A class whose overlays do not state the fleet class takes it, and the signed TLV, as defines.
"""
import hashlib
import json

FEATURES = {
    # feature -> overlays appended to the class's
    "brain": ["overlay-attachment-brain.conf"],
    # bring-up: the LoRa transceiver driver at DEBUG (its interrupts and configuration on
    # the console); a feature so the image has its own identity, never the plain class's
    "lora-debug": ["overlay-lora-debug.conf"],
}

APP = "samples/meshtastic"


class RecipeError(ValueError):
    pass


def overlays_for(cls, features=()):
    out = list(cls.get("overlays") or [])
    for f in features or ():
        if f not in FEATURES:
            raise RecipeError(f"unknown feature {f!r} (known: {sorted(FEATURES)})")
        for o in FEATURES[f]:
            if o not in out:
                out.append(o)
    return out


def defines_for(class_id, cls, features, tree, version, key_file):
    """The -D settings, as an ordered list of (name, value) with values exactly as CMake sees them."""
    d = []
    if cls.get("shield"):
        d.append(("SHIELD", cls["shield"]))
    d.append(("EXTRA_CONF_FILE", ";".join(overlays_for(cls, features))))
    if cls.get("dtc_overlays"):
        d.append(("EXTRA_DTC_OVERLAY_FILE", ";".join(cls["dtc_overlays"])))
    for k, v in (cls.get("sysbuild") or {}).items():
        d.append((f"SB_CONFIG_{k}", "y" if v is True else "n" if v is False else str(v)))
    if cls.get("signed", True):
        if not key_file:
            raise RecipeError("a signed class needs the signing key path")
        d.append(("SB_CONFIG_BOOT_SIGNATURE_KEY_FILE", f'"{key_file}"'))
    if cls.get("class_in_recipe"):
        d.append(("CONFIG_MESHTASTIC_FLEET_CLASS", str(int(class_id))))
        d.append(("CONFIG_MCUBOOT_EXTRA_IMGTOOL_ARGS",
                  f'"--custom-tlv 0x00A0 0x{int(class_id):02x}"'))
    for k, v in (cls.get("defines") or {}).items():
        d.append((k, str(v)))
    d.append(("CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION", f'"{sign_version(version)}"'))
    d.append(("ZEPHYR_EXTRA_MODULES", str(tree)))
    return d


def sign_version(version):
    """'0.4.38' -> '0.4.38+0'; a version that already carries a build number is kept."""
    v = str(version)
    parts = v.split("+")[0].split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise RecipeError(f"version must be MAJOR.MINOR.PATCH[+BUILD], got {version!r}")
    return v if "+" in v else v + "+0"


def render(class_id, cls, tree, version, build_dir, features=(), key_file=None):
    """The west build argv for one image."""
    for need in ("board", "flasher"):
        if not cls.get(need):
            raise RecipeError(f"class {class_id}: missing {need!r}")
    argv = ["west", "build", "-d", str(build_dir), "--sysbuild", "-b", cls["board"],
            f"{tree}/{APP}", "--"]
    argv += [f"-D{k}={v}" for k, v in defines_for(class_id, cls, features, tree, version, key_file)]
    return argv


def image_key(class_id, cls, commit, version, features=()):
    """Identity of the image: source commit + everything the recipe contributes + version.

    The tree PATH and the key PATH are not part of it (two checkouts of one commit build the
    same image); the build dir is not either.
    """
    doc = {
        "class": int(class_id),
        "commit": commit,
        "version": sign_version(version),
        "defines": [(k, v) for k, v in
                    defines_for(class_id, cls, features, "<tree>", version, "<key>")],
        "board": cls["board"],
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]


def artifact(cls):
    """What the build leaves behind that gets flashed, relative to the build dir."""
    if cls["flasher"] == "uf2":
        return {"kind": "uf2", "hex": cls.get("merged_hex", "merged_xiao_ble_nrf52840.hex"),
                "family": "0xADA52840", "signed_bin": "meshtastic/zephyr/zephyr.signed.bin"}
    if cls["flasher"] == "esptool":
        return {"kind": "bin", "signed_bin": "meshtastic/zephyr/zephyr.signed.bin"}
    raise RecipeError(f"unknown flasher {cls['flasher']!r}")
