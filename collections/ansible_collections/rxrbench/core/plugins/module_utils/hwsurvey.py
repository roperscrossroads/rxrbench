"""What a USB device on a bench host is, from what it says about itself.

Three layers, each usable alone:

- `classify(usb)`: VID:PID and descriptor strings -> kind, mode and the identifiers the
  descriptors already carry, without touching the device. On an ESP32-S3's USB-Serial-JTAG
  the serial is the base MAC; on an nRF52840 it is the chip's FICR device id, the same
  in every mode (an app, its UF2 bootloader, the factory app). So the passive pass alone
  learns a stable identity for both.
- `tags(record)`: capability tags from a classified record plus whatever probes added
  (`mcu:esp32s3`, `psram:8MB`, `bootloader:uf2-0.6.1`, `fw:<family>`, ...). Tags are
  derived, never typed in; a registry entry's own `tags:` are added on top by the caller.
- `match_registry(usb, devices)`: which registry entry, if any, is this device.

Parsers for what the probes print (`parse_info_uf2`, `parse_esptool`, `parse_espefuse`) are
here too, so they are unit-tested against real output.
"""
import re

#: Probes that are not a firmware answering: the chip's ROM, a UF2 bootloader's drive, and
#: `boot_switch` (`just boot` replaced the loader; the app it ran no longer boots).
HARDWARE_PROBES = ("esptool", "bootloader", "boot_switch", "probe_drive", "swd")

# VID:PID -> what it is. `mode` is the device's state, not its kind: one board may have a row
# for its bootloader and one for its app. `serial_is` names what the USB serial is, where it
# is meaningful. The rows here are a chip vendor's own ids, USB-UART bridges and debug probes:
# what any bench may meet. A board vendor's ids are the plugin's whose board it is
# (`register_kinds`).
KINDS = {
    ("303a", "1001"): {"kind": "esp32-usb-jtag", "mode": "app-or-rom", "serial_is": "mac",
                       "note": "Espressif USB-Serial-JTAG (an S2, S3, C3 or C6 alike): same VID:PID "
                               "in the app and in ROM download mode; the serial is the base MAC. "
                               "Which chip is the deep probe's to say, or the board type's"},
    ("303a", "0002"): {"kind": "esp32-usb-cdc", "mode": "app", "serial_is": "mac_compact",
                       "note": "an ESP32-S2/S3 app's own USB CDC (TinyUSB), e.g. a vendor's factory "
                               "app; the serial is the base MAC without colons. A deep probe "
                               "resets it into ROM download mode (303a:1001) and back"},
    ("2fe3", "0004"): {"kind": "zephyr-cdc", "mode": "app", "serial_is": "chip_id",
                       "note": "a Zephyr app's CDC-ACM console (the serial is its hwinfo device "
                               "id: an nRF52's FICR id, an RP2040's flash id, an RP2350's chip id)"},
    ("2fe3", "0100"): {"kind": "zephyr-cdc", "mode": "app", "serial_is": "chip_id",
                       "note": "a Zephyr app's CDC-ACM console (Zephyr's default ids)"},
    ("10c4", "ea60"): {"kind": "uart-bridge", "bridge": "cp210x", "mode": "bridge",
                       "note": "CP210x USB-UART: the board behind it is unknown from USB"},
    ("1a86", "7523"): {"kind": "uart-bridge", "bridge": "ch340", "mode": "bridge",
                       "note": "CH340 USB-UART: no serial number; the board behind it is "
                               "unknown from USB"},
    ("1a86", "55d4"): {"kind": "uart-bridge", "bridge": "ch9102", "mode": "bridge"},
    ("0483", "3748"): {"kind": "swd-probe", "probe": "stlink-v2", "mode": "probe"},
    ("0483", "374b"): {"kind": "swd-probe", "probe": "stlink-v2-1", "mode": "probe"},
    ("0483", "374e"): {"kind": "swd-probe", "probe": "stlink-v3", "mode": "probe"},
    ("0483", "374f"): {"kind": "swd-probe", "probe": "stlink-v3", "mode": "probe"},
    ("0483", "3753"): {"kind": "swd-probe", "probe": "stlink-v3", "mode": "probe"},
    ("0483", "3754"): {"kind": "swd-probe", "probe": "stlink-v3", "mode": "probe"},
    ("0d28", "0204"): {"kind": "swd-probe", "probe": "cmsis-dap", "mode": "probe"},
    # The RP2040's and RP2350's ROM bootloader (BOOTSEL): a drive and PICOBOOT. An RP2040's
    # serial is not the flash id its applications carry, so it names no unit by itself
    # (picotool reads the flash id, flash/picoboot.py); an RP2350's is its chip id, the serial
    # its applications give too.
    ("2e8a", "0003"): {"kind": "rp2-bootrom", "mcu": "rp2040", "mode": "bootloader",
                       "note": "RP2040 ROM bootloader (BOOTSEL): picotool finds the chip by its "
                               "flash id, not by this serial"},
    ("2e8a", "000f"): {"kind": "rp2-bootrom", "mcu": "rp2350", "mode": "bootloader",
                       "serial_is": "chip_id",
                       "note": "RP2350 ROM bootloader (BOOTSEL): the serial is the chip id in OTP"},
    # A pico-sdk application's own USB (stdio over USB, with picotool's reset interface): the
    # serial is the flash id.
    ("2e8a", "000a"): {"kind": "rp2-app", "mode": "app", "serial_is": "chip_id",
                       "note": "a pico-sdk application's USB (stdio, reset interface); the serial "
                               "is the chip's flash id"},
    ("2e8a", "000c"): {"kind": "swd-probe", "probe": "cmsis-dap", "mode": "probe",
                       "note": "Raspberry Pi Debug Probe / picoprobe"},
}
_CORE_KINDS = dict(KINDS)


def register_kinds(rows):
    """Take the loaded plugins' USB ids ({"vid:pid": {kind, mcu, mode, serial_is, note}}): the
    table is this module's own rows and theirs. A plugin's row replaces one of these: the rows
    here are what a class of device does by default (a Zephyr app's serial is its chip id), and
    a plugin whose firmware does otherwise with the same ids says so. Between two plugins'
    rows for one id, the first loaded stands."""
    KINDS.clear()
    KINDS.update(_CORE_KINDS)
    taken = set()
    for key, row in (rows or {}).items():
        vid, _, pid = key.lower().partition(":")
        if (vid, pid) not in taken:
            KINDS[(vid, pid)] = dict(row)
            taken.add((vid, pid))


# Not boards: hubs, the bench hosts' own disks and radios. Reported, never offered for
# registration as a board.
INFRA_CLASSES = {"09"}   # bDeviceClass hub
# eFuses worth keeping per board: what the chip is (package, embedded flash/PSRAM, wafer
# revision), a unique id, and the security state that decides what a flash may do.
EFUSE_KEEP = ("MAC", "OPTIONAL_UNIQUE_ID", "CHIP_PACKAGE", "WAFER_VERSION_MAJOR",
              "WAFER_VERSION_MINOR", "BLK_VERSION_MAJOR", "BLK_VERSION_MINOR", "FLASH_CAP",
              "FLASH_VENDOR", "FLASH_TEMP", "PSRAM_CAP", "PSRAM_VENDOR", "PSRAM_TEMP",
              "SECURE_BOOT_EN", "SPI_BOOT_CRYPT_CNT", "DIS_DOWNLOAD_MODE", "DIS_USB_JTAG",
              "DIS_USB_SERIAL_JTAG", "SOFT_DIS_JTAG", "ENABLE_SECURITY_DOWNLOAD",
              # the original ESP32's names for the same three: secure boot v1 and v2, the
              # flash-encryption counter (on while an odd number of its bits are set), and
              # UART download mode disabled
              "ABS_DONE_0", "ABS_DONE_1", "FLASH_CRYPT_CNT", "UART_DOWNLOAD_DIS")

def rom_port_path(mac):
    """The by-id path an ESP32-S2/S3 has in ROM download mode (303a:1001): its serial is the base
    MAC with colons, whatever the app's own USB identity was."""
    return f"/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_{mac.upper()}-if00"


def efuse_all(summary):
    """`espefuse summary --format json` -> (every decoded field as name: value, the state of the
    key and user-data blocks). The raw key and user-data blocks can hold secrets (a burned
    flash-encryption or secure-boot key, customer data), so their bytes are never kept: only
    whether each is empty. Everything else is what the chip itself decodes: MAC, unique id,
    ADC and temperature calibration, regulator trims, package, security state."""
    fields, blocks = {}, {}
    for name, rec in (summary or {}).items():
        if not isinstance(rec, dict) or "value" not in rec:
            continue
        value = rec["value"]
        if name.startswith(("BLOCK_KEY", "BLOCK_USR")):
            raw = str(value).replace(" ", "")
            blocks[name] = "empty" if raw and set(raw) <= {"0"} else (
                "burned" if raw and "?" not in raw else "unreadable")
            continue
        fields[name] = value if rec.get("readable", True) else "unreadable"
    return fields, blocks


MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def classify(usb):
    """A sysfs USB record (usb_vid, usb_pid, serial, product, manufacturer, ...) -> what it is."""
    vid, pid = (usb.get("usb_vid") or "").lower(), (usb.get("usb_pid") or "").lower()
    known = KINDS.get((vid, pid))
    out = {"kind": "unknown", "mode": None}
    if usb.get("bDeviceClass") in INFRA_CLASSES:
        return {"kind": "hub", "mode": None, "infra": True}
    if known is None:
        if (usb.get("manufacturer") or "").startswith("Linux") or vid == "1d6b":
            return {"kind": "root-hub", "mode": None, "infra": True}
        if is_input(usb):
            return {"kind": "input", "mode": None, "note": "a keyboard or mouse (HID only)"}
        return out
    out = {k: v for k, v in known.items() if k != "serial_is"}
    serial = usb.get("serial") or ""
    ids = {}
    if known.get("serial_is") == "mac" and MAC_RE.match(serial):
        ids["mac"] = serial.upper()
    elif known.get("serial_is") == "mac_compact" and re.fullmatch(r"[0-9A-Fa-f]{12}", serial):
        ids["mac"] = ":".join(serial[i:i + 2] for i in range(0, 12, 2)).upper()
    elif known.get("serial_is") == "chip_id" and re.fullmatch(r"[0-9A-F]{16}", serial):
        ids["chip_id"] = serial
    if ids:
        out["ids"] = ids
        # Read off the USB serial by the row's rule, not asked of the chip: a deep probe or the
        # firmware's identity answer is what confirms it.
        out["ids_from"] = "usb serial"
    if known.get("kind") == "uart-bridge" and serial.isdigit():
        # Every bridge of the model ships with this serial: two of them cannot be told apart,
        # and the plug watcher cannot see the board behind one by it.
        out["note"] = (out.get("note", "USB-UART bridge") + f"; serial {serial} is the factory "
                       "default, shared by every bridge of its model: give it a serial of its "
                       "own (`just cp210x-serial <host>`, core/host/bin/cp210x_serial.py) so the board behind it can be found")
    return out


def is_input(usb):
    """A keyboard or mouse: every interface HID (class 03), one of them a boot keyboard or boot
    mouse (subclass 01, protocol 01 or 02). A HID-only bootloader (a Teensy's) declares no boot
    protocol, so a board in one is still a device to report."""
    ifs = usb.get("interfaces") or []
    return bool(ifs) and all(i.startswith("03/") for i in ifs) and \
        any(i in ("03/01/01", "03/01/02") for i in ifs)


def match_registry(usb, devices, by_ids=(), host=None, siblings=(), block_ids=()):
    """The registry names whose `match` this USB device satisfies. More than one is the
    registry's ambiguity, reported as such, never resolved by a guess.

    Match keys as the registry uses them (docs/REGISTRY.md), compared the way devreg compares
    them, case-insensitively: `usb_serial` (a chip id, in every mode's descriptor),
    `serial_by_id` (a /dev/serial/by-id basename: USB-JTAG boards and bridges), `block_by_id`,
    `usb_vid` / `usb_pid`, `host` (where that entry's device lives) and `sibling_*` (a bridge
    with no serial counts only beside a device with those ids on the same hub: `siblings` are
    the other USB devices seen on the host).

    An entry that names this device by a distinguishing key (a serial, a by-id name, a
    sibling) outranks one that matches it by make alone (VID:PID), as in devreg: a CH340 beside
    its board's own RP2040 is that board, not every CH340 board in the registry."""
    named, by_make = [], []
    for name, entry in (devices or {}).items():
        if (entry or {}).get("kind") == "block":
            continue
        # `match` is the board's lasting USB face, `match_also` its other ones (a stock app
        # before the first flash): a hit on any face is the board.
        hit = [m for m in [(entry or {}).get("match") or {}] + list((entry or {}).get("match_also") or [])
               if m and _block_matches(m, usb, by_ids, host, siblings, block_ids)]
        if any(distinguishes(m) for m in hit):
            named.append(name)
        elif hit:
            by_make.append(name)
    return sorted(named or by_make)


# The keys that tell one device from another of the same make (devreg's DISTINGUISHING).
DISTINGUISHING = ("usb_serial", "serial_by_id", "block_by_id",
                  "sibling_vid", "sibling_pid", "sibling_serial")


def distinguishes(m):
    return any(k in m for k in DISTINGUISHING)


def ambiguous_names(records):
    """Registry names that more than one surveyed device took: name -> their USB bus ids.
    One entry, two devices (two bridges of the same make, matched by VID:PID alone): neither
    is that board until the registry can tell them apart, as devreg refuses them."""
    seen = {}
    for rec in records:
        for name in rec.get("registered") or ():
            seen.setdefault(name, []).append(rec["usb"].get("busid") or rec["usb"].get("syspath"))
    return {n: sorted(b) for n, b in seen.items() if len(b) > 1}


def _lower(v):
    return str(v).lower()


def _same_hub(a, b):
    """Two USB devices behind the same parent hub: by sysfs path when both carry one, else by
    the plug watcher's bus id (`1-1.4.2`: the parent is `1-1.4`)."""
    if a.get("syspath") and b.get("syspath"):
        return a["syspath"] != b["syspath"] and \
            a["syspath"].rsplit("/", 1)[0] == b["syspath"].rsplit("/", 1)[0]
    if a.get("busid") and b.get("busid"):
        return a["busid"] != b["busid"] and \
            a["busid"].rsplit(".", 1)[0] == b["busid"].rsplit(".", 1)[0]
    return False


def _block_matches(m, usb, by_ids, host, siblings=(), block_ids=()):
    m = {k: _lower(v) for k, v in m.items()}
    serial = _lower(usb.get("serial") or "")
    tests = 0
    if "usb_serial" in m:
        tests += 1
        if m["usb_serial"] != serial:
            return False
    if "serial_by_id" in m:
        tests += 1
        if m["serial_by_id"] not in {_lower(x) for x in by_ids}:
            return False
    if "block_by_id" in m:
        tests += 1
        if m["block_by_id"] not in {_lower(x) for x in block_ids}:
            return False
    for k in ("usb_vid", "usb_pid"):
        if k in m:
            tests += 1
            if m[k] != _lower(usb.get(k) or ""):
                return False
    if tests == 0:
        return False
    want = {k[len("sibling_"):]: v for k, v in m.items() if k.startswith("sibling_")}
    if want and not any(_same_hub(usb, s) and all(
            _lower({"vid": s.get("usb_vid"), "pid": s.get("usb_pid"), "serial": s.get("serial")}.get(k) or "") == v
            for k, v in want.items()) for s in siblings):
        return False
    return host is None or m.get("host") in (None, _lower(host))


def has_data(probe):
    """A probe result with something in it besides its own error bookkeeping."""
    return any(k not in ("gathered", "last_error", "last_error_at", "via") for k in probe or {})


def parse_probe_drive(label, details, mbed):
    """A board's built-in debug probe's drive (an ST-Link V2-1 on a Nucleo or Discovery) ->
    {label, firmware, build, mbed_code}: the volume label names the board (NOD_F767ZI),
    DETAILS.TXT the probe's firmware, MBED.HTM a board code (its first four hex digits are the
    board, 0818 a NUCLEO-F767ZI). Only what was there."""
    out = {"label": label} if label else {}
    for key, pat in (("firmware", r"^Version:\s*(\S+)"), ("build", r"^Build:\s*(.+?)\s*$")):
        m = re.search(pat, details or "", re.M)
        if m:
            out[key] = m.group(1)
    m = re.search(r"code=([0-9A-Fa-f]{4,})", mbed or "")
    if m:
        out["mbed_code"] = m.group(1)
    return out


def board_of_drive(label, boards):
    """The board type (catalog `boards`) that claims this probe drive label, or None."""
    hits = [n for n, b in sorted((boards or {}).items()) if label and (b or {}).get("probe_drive") == label]
    return hits[0] if len(hits) == 1 else None


def parse_info_uf2(text):
    """INFO_UF2.TXT from a UF2 bootloader's drive -> {version, model, board_id, softdevice}."""
    out = {}
    m = re.search(r"UF2 Bootloader (\S+)", text)
    if m:
        out["version"] = m.group(1)
    for key, pat in (("model", r"^Model:\s*(.+)$"), ("board_id", r"^Board-ID:\s*(.+)$"),
                     ("softdevice", r"^SoftDevice:\s*(.+)$"), ("date", r"^Date:\s*(.+)$")):
        m = re.search(pat, text, re.M)
        if m:
            out[key] = m.group(1).strip()
    return out


def parse_esptool(text):
    """`esptool flash_id` output -> chip, revision, features, crystal, MAC, flash."""
    out = {}
    m = re.search(r"Chip is (\S+) \(revision v?([\d.]+)\)", text) or \
        re.search(r"Chip type:\s+(\S+).*?\(revision v?([\d.]+)\)", text, re.S)
    if m:
        out["chip"], out["revision"] = m.group(1), m.group(2)
    m = re.search(r"^Features:\s*(.+)$", text, re.M)
    if m:
        out["features"] = [f.strip() for f in m.group(1).split(",")]
    m = re.search(r"^Crystal (?:is|frequency):\s*(\S+)", text, re.M)
    if m:
        out["crystal"] = m.group(1)
    m = re.search(r"^(?:MAC|BASE MAC):\s*([0-9a-fA-F:]{17})", text, re.M)
    if m:
        out["mac"] = m.group(1).upper()
    m = re.search(r"Manufacturer:\s*([0-9a-fA-F]+)", text)
    if m:
        out["flash_manufacturer"] = m.group(1).lower()
    m = re.search(r"Device:\s*([0-9a-fA-F]+)", text)
    if m:
        out["flash_device"] = m.group(1).lower()
    m = re.search(r"Detected flash size:\s*(\S+)", text)
    if m:
        out["flash_size"] = m.group(1)
    return out


def psram_mb(features):
    for f in features or []:
        m = re.search(r"(?:Embedded )?PSRAM (\d+)MB", f)
        if m:
            return int(m.group(1))
    return None


def tags(record, boards=None, family_tags=None):
    """Capability tags for one surveyed device, sorted. Only what the record shows.
    `family_tags(probes, boards)` gives the tags of the firmware probes, which are the
    families' to read."""
    t = set()
    c = record.get("class") or {}
    if c.get("mcu"):
        t.add(f"mcu:{c['mcu']}")
    if c.get("probe"):
        t.update({"tool:swd-probe", f"probe:{c['probe']}"})
    if c.get("bridge"):
        t.add(f"uart-bridge:{c['bridge']}")
    mode = c.get("mode")
    # A firmware that answered (any family's probe) means the board is in its app, not its ROM.
    if mode == "app-or-rom" and any(has_data(v) for k, v in (record.get("probes") or {}).items()
                                     if k not in HARDWARE_PROBES):
        mode = "app"
    if mode in ("app", "bootloader", "stock-app", "probe"):
        t.add(f"usb-mode:{mode}")
    if c.get("kind") == "esp32-usb-jtag":
        t.add("usb:native-jtag")
    elif c.get("kind") == "esp32-usb-cdc":
        t.add("usb:native-cdc")
    probes = {k: v for k, v in (record.get("probes") or {}).items() if has_data(v)}
    record = {**record, "probes": probes}
    esp = (record.get("probes") or {}).get("esptool") or {}
    if esp.get("chip"):
        t.add("mcu:" + esp["chip"].lower().replace("-", ""))
    feats = " ".join(esp.get("features") or [])
    if "WiFi" in feats or "Wi-Fi" in feats:
        t.add("radio:wifi")
    if "BLE" in feats or "BT 5" in feats or "(LE)" in feats:
        t.add("radio:ble")
    mb = psram_mb(esp.get("features"))
    if mb:
        t.add(f"psram:{mb}MB")
    if esp.get("flash_size"):
        t.add(f"flash:{esp['flash_size']}")
    if esp.get("revision"):
        t.add(f"chip-rev:v{esp['revision']}")
    ef = esp.get("efuse") or {}
    # What a bootloader write may do: secure boot or flash encryption on means only signed
    # (or encrypted) images will run, and ROM download off means no wired recovery.
    for key, tag in (("SECURE_BOOT_EN", "secure-boot"), ("DIS_DOWNLOAD_MODE", "rom-download")):
        if key in ef:
            on = ef[key] is True or str(ef[key]).lower() in ("true", "1")
            t.add(f"{tag}:{'off' if on else 'on'}" if key == "DIS_DOWNLOAD_MODE"
                  else f"{tag}:{'on' if on else 'off'}")
    if "SPI_BOOT_CRYPT_CNT" in ef:
        t.add("flash-encryption:" + ("off" if str(ef["SPI_BOOT_CRYPT_CNT"]).startswith("Disable")
                                     else "on"))
    # The original ESP32 names them otherwise. Facts surveyed before these were kept have them
    # only in efuse_all.
    classic = {**(esp.get("efuse_all") or {}), **ef}
    if "ABS_DONE_0" in classic or "ABS_DONE_1" in classic:
        on = any(classic.get(k) is True or str(classic.get(k)).lower() in ("true", "1")
                 for k in ("ABS_DONE_0", "ABS_DONE_1"))
        t.add(f"secure-boot:{'on' if on else 'off'}")
    if "FLASH_CRYPT_CNT" in classic:
        try:
            on = bin(int(str(classic["FLASH_CRYPT_CNT"]), 0)).count("1") % 2 == 1
        except ValueError:
            on = True               # a value this cannot read is not proof it is off
        t.add(f"flash-encryption:{'on' if on else 'off'}")
    if "UART_DOWNLOAD_DIS" in classic:
        off = classic["UART_DOWNLOAD_DIS"] is True or str(classic["UART_DOWNLOAD_DIS"]).lower() in ("true", "1")
        t.add(f"rom-download:{'off' if off else 'on'}")
    # An app manager in its own partition, with the images it installed beside it: a write at
    # 0x0 or into its slots orphans them all, so every flasher reads this tag first.
    mgr = (esp.get("partitions") or {}).get("manager")
    if mgr:
        t.add(f"app-manager:{mgr['label']}")
        t.add(f"app-manager-apps:{len(mgr.get('apps') or [])}")
    # What each firmware's probe says is its family's to tag (the controller passes the
    # plugins' answer; a module on a bench host has none to pass).
    if family_tags:
        t |= family_tags(probes, boards)
    drive = (record.get("probes") or {}).get("probe_drive") or {}
    if drive.get("label"):
        t.add(f"probe-drive:{drive['label']}")
        if board_of_drive(drive["label"], boards):
            t.add(f"board-type:{board_of_drive(drive['label'], boards)}")
    if drive.get("firmware"):
        t.add(f"probe-firmware:{drive['firmware']}")
    bl = (record.get("probes") or {}).get("bootloader") or {}
    if bl.get("version"):
        t.add(f"bootloader:uf2-{bl['version']}")
    if bl.get("softdevice"):
        sd = re.sub(r"\s+version\s+", "-", bl["softdevice"]).lower().replace(" ", "-")
        t.add(f"softdevice:{sd}")
    return sorted(t)


def facts_key(record, host=None):
    """The file a device's facts are kept under: its registry name, or a name made from
    what it is, which stays the same across plug-ins (never a /dev path). A device whose
    descriptors carry no chip id or MAC is filed under its host and USB port too: a USB
    serial alone is not unique (two identical disk enclosures can report the same one)."""
    names = record.get("registered") or []
    if len(names) == 1:
        return names[0]
    ids = (record.get("class") or {}).get("ids") or {}
    ident = ids.get("chip_id") or (ids.get("mac") or "").replace(":", "").lower()
    if not ident:
        ident = "-".join(x for x in (host, record["usb"]["busid"],
                                     record["usb"].get("serial")) if x)
    ident = re.sub(r"[^A-Za-z0-9-]+", "", str(ident)) or "noserial"
    return f"new-{record['usb']['usb_vid']}{record['usb']['usb_pid']}-{ident}"
