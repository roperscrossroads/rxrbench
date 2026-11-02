#!/usr/bin/env python3
"""Flash a board through a UF2 bootloader (a mass-storage drive an image is copied to), on
whichever host has the board attached over USB: resolve by the board's own USB serial, never by
port, never by host.

Nothing here names a board or a chip. What differs between board types is data, read from the
board catalog (catalog/boards.yml, the `uf2:` block of a type):

    bootloader_usb   the USB VID:PID of the board's bootloader (how a bootloader is told from an app)
    app              [lowest, highest) address an application UF2 may write
    family           the UF2 family id an application image carries (or a list of them)
    touch            what a 1200-baud touch on the app's port does: `dfu` (the bootloader's serial
                     DFU, no drive: the Adafruit nRF52 bootloader), `uf2` (its drive appears), or
                     absent (nothing known: never sent)
    dfu              the tool for the bootloader's serial DFU (`adafruit-nrfutil`), or absent
    drive_serial     `chip` (default): the drive's USB serial is the board's, which is what tells
                     two drives of one type apart; `none`: it is not (an RP2040's ROM drive), so
                     this tool refuses the type and its own flasher writes it
    drive_max_mb     the largest the drive may be (default 64): a guard against a wrong disk
    manual           what someone does to bring the drive up by hand (default: double-tap reset)
    refuse_families  {family id: what such a file is}: UF2 families the bootloader takes that are
                     not an application (its own update, a factory erase), refused by name
    drive_identity   {file, pattern}: a file on the drive in which the bootloader names its chip
                     (the pattern's one group is the chip id). Where the drive has the file, the id
                     must be the board's; a drive without it is identified by its USB serial alone

A board's type is the registry's `policy.board_tag`, or `--type <tag>` / UF2_BOARD_TYPE when
the registry does not say (the two must agree when both are given).

The script assumes no fixed location for itself or for adafruit-nrfutil. Run it on the
machine the board is plugged into; it has no ssh or host dispatch.

It follows devreg's contract without importing devreg: identical boards differ only by serial,
and a ttyACM number is not an identity. Every mode resolves a name to a chip serial and then
looks in /dev/serial/by-id/ for it; absent and ambiguous are both errors.

  uf2_flash.py list
  uf2_flash.py resolve <name>
  uf2_flash.py touch    <name>              app -> bootloader (1200-baud touch)
  uf2_flash.py dfu      <name> <pkg.zip>    touch if needed, then serial DFU
  uf2_flash.py enter-choice <name>          how uf2-mode would ask it, said and nothing sent
                                            (a plan asks this); refused as uf2-mode would be
  uf2_flash.py uf2-mode <name>              bring the board to its bootloader's UF2 mode
                                            (mass-storage drive) and print the drive
  uf2_flash.py uf2      <name> <app.uf2>    uf2-mode, then copy an app UF2 to the drive
  uf2_flash.py app-back <name>              a board left in UF2 mode back to the app it holds:
                                            the app-window blocks of its own CURRENT.UF2,
                                            copied back (the same bytes; the copy is the reset)
  uf2_flash.py verify-drive <name> <mount>  the drive guard on its own
  (any of them: --type <board type> before the command)

How the drive is brought up depends on the board type and on the firmware it runs
(UF2_ENTER says which; `auto` is the default):

    auto     a board type whose touch gives the drive is touched; else the app is asked in the
             way of one of the families in UF2_ENTERS (a JSON list of {family, shell_command,
             shell_usb, app_tools}, in order): on its shell when its USB vendor (or
             vendor:product) is one the family's shell runs on (shell_usb, default 2fe3,
             Zephyr's), else by the first of the families' own tools present here (a command
             line, `{port}` is the app's port). The flasher sets the list: the entry of the
             family the board was read as (UF2_RUNS), or of the one the user named
             (UF2_ENTER_AS, from bench_uf2_enter_as), else every family's, the image's first.
             Two families' shells on the app's vendor with different commands is refused:
             which firmware it runs must be named (docs/ARCHITECTURE.md#entering-uf2-mode)
    manual   nobody is asked: the drive is waited for (UF2_MANUAL_WAIT seconds, default 90)
             while someone does what the type's `manual` says. For firmware with no command
             for it, and for a board whose app does not answer.

On the Adafruit nRF52 bootloader a touch gives serial-only DFU, not UF2 mode
(docs/HARDWARE.md, "nRF52840 with a UF2 bootloader"): its types say `touch: dfu`.

A board already showing its drive is used as it is, whatever UF2_ENTER says. A board in
serial-only DFU cannot be moved to UF2 mode in software; it needs a replug first.

This file is the source; each bench host runs a deployed copy at ~/rxrbench/bin/uf2_flash.py
(role bench_host, with the board catalog in ~/rxrbench/catalog/). An edit made on a host is
reverted by the next deploy.

A bootloader is matched by its own USB VID:PID, never as "whatever is not the app" and never by
a product name: a stock app may carry the bootloader's product string, and an app taken for a
bootloader never answers DFU (docs/HARDWARE.md, "nRF52840 with a UF2 bootloader").
"""
import glob, json, os, re, shlex, shutil, subprocess, sys, time

# `serial` (pyserial, needed by `touch`/`dfu` only) usually lives in a venv, not the system
# Python: when it is missing, the script re-execs once under the default install's venv.
try:
    import serial as _pyserial  # noqa: F401
except ImportError:
    if os.environ.get("UF2_FLASH_REEXEC") != "1":
        for candidate in (
            "~/.venv-mcumgr/bin/python3",              # the default install's venv
        ):
            p = os.path.expanduser(candidate)
            if os.path.exists(p):
                os.environ["UF2_FLASH_REEXEC"] = "1"
                os.execv(p, [p, __file__] + sys.argv[1:])
    if "touch" in sys.argv or "dfu" in sys.argv:
        sys.exit("uf2_flash: 'serial' (pyserial) not importable on this host and no known "
                 "venv found to re-exec under -- pip install pyserial, activate a venv "
                 "with it, or add this host's venv to the candidate list above")

# Board names -> USB serials come from devreg's registry alone (`devreg deploy` puts it on each
# bench host): a chip id (an nRF52's FICR id) or another serial that names the unit, the same in
# its app and in its bootloader.
REGISTRY = os.environ.get("DEVREG_REGISTRY", os.path.expanduser("~/.devreg/registry.yaml"))
# Board types: the tool's, each plugin's and the site's, merged into one file by the role
# bench_host; a second file in the list, when present, is read over it.
BOARDS = [os.path.expanduser(p) for p in os.environ.get(
    "UF2_BOARDS", "~/rxrbench/catalog/boards.yml:~/rxrbench/catalog/site/boards.yml").split(":") if p]
# Set by `--type` (main) or UF2_BOARD_TYPE: the board's type when the registry does not say.
BOARD_TYPE = os.environ.get("UF2_BOARD_TYPE") or None


_YAML = {}


def _yaml(path):
    """A YAML file as Python data. The interpreter this runs under reads it when it has PyYAML
    (the tool's venvs do); else the host's own python3, which devreg needs it for. A file
    neither can read stops the run with the reason, rather than passing as an empty catalog
    (in which no board type would have a uf2 block)."""
    if path not in _YAML:                    # read once a run: the wait loops ask often
        try:
            import yaml  # pylint: disable=import-outside-toplevel
            with open(path) as f:
                _YAML[path] = yaml.safe_load(f) or {}
        except ImportError:
            r = subprocess.run(["python3", "-c", "import json, sys, yaml; print(json.dumps("
                                "yaml.safe_load(open(sys.argv[1])) or {}, default=str))", path],
                               capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit(f"uf2_flash: cannot read {path}: neither this interpreter nor python3 "
                         f"has PyYAML ({r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'no output'})")
            _YAML[path] = json.loads(r.stdout)
    return _YAML[path]


#: A USB serial that names one unit: long enough not to be found inside another's by-id name,
#: and not a factory default every device of a model shares (all digits, like a bridge's 0001).
UNIT_SERIAL = re.compile(r"(?=.*[A-Za-z])[0-9A-Za-z]{10,}|[0-9A-F]{16}")


def registry_boards():
    """Entries of devreg's registry on this host that a unit serial names: name (and each alias)
    -> {serial: `match.usb_serial`, board_tag: `policy.board_tag`}."""
    out = {}
    for name, e in (_yaml(REGISTRY).get("devices") or {}).items():
        e = e or {}
        s = str((e.get("match") or {}).get("usb_serial") or "")
        if UNIT_SERIAL.fullmatch(s):
            for n in [name, *(e.get("aliases") or [])]:
                out[str(n)] = {"serial": s, "board_tag": (e.get("policy") or {}).get("board_tag")}
    return out


def kit_serial(name):
    """The chip id for a board name, from the registry."""
    reg = registry_boards().get(name)
    if reg:
        return reg["serial"]
    sys.exit(f"unknown board '{name}': no unit usb_serial for it in "
             f"{REGISTRY} -- known: {', '.join(sorted(registry_boards()))}")


def all_kits():
    return {n: e["serial"] for n, e in registry_boards().items()}


def board_types():
    """{type: its `uf2:` block, checked} for every board type of the catalog that has one."""
    types = {}
    for path in BOARDS:
        if os.path.exists(path):
            types.update(_yaml(path).get("boards") or {})
    out = {}
    for tag, t in types.items():
        u = (t or {}).get("uf2")
        if not u:
            continue
        try:
            usb = {tuple(str(x).lower().split(":")) for x in u["bootloader_usb"]}
            lo, hi = (int(x) for x in u["app"])
            fam = frozenset(int(f) for f in (u["family"] if isinstance(u["family"], list)
                                             else [u["family"]]))
        except (KeyError, TypeError, ValueError) as e:
            sys.exit(f"board type {tag!r}: its uf2 block is not {{bootloader_usb: [vid:pid], "
                     f"app: [lo, hi], family}} ({type(e).__name__}: {e})")
        if not usb or any(len(x) != 2 for x in usb) or not 0 < lo < hi:
            sys.exit(f"board type {tag!r}: its uf2 block names no bootloader or no app region")
        touch, dfu = u.get("touch"), u.get("dfu")
        drive_serial = u.get("drive_serial", "chip")
        if touch not in (None, "dfu", "uf2") or dfu not in (None, "adafruit-nrfutil") \
                or drive_serial not in ("chip", "none"):
            sys.exit(f"board type {tag!r}: uf2 touch is dfu|uf2, dfu is adafruit-nrfutil, "
                     f"drive_serial is chip|none (got {touch!r}, {dfu!r}, {drive_serial!r})")
        try:
            # keys arrive as ints, or as strings once the catalog has been through JSON or a
            # hand-written YAML string ("0xD663823C")
            refuse = {int(str(f), 0): str(why) for f, why in (u.get("refuse_families") or {}).items()}
            ident = u.get("drive_identity")
            if ident is not None:
                ident = {"file": str(ident["file"]), "pattern": re.compile(ident["pattern"], re.M)}
                if ident["pattern"].groups != 1 or os.sep in ident["file"]:
                    raise ValueError("drive_identity is {file: a name on the drive, pattern: "
                                     "one group, the chip id}")
        except (KeyError, TypeError, ValueError, re.error) as e:
            sys.exit(f"board type {tag!r}: uf2 refuse_families is {{family: why}}, drive_identity "
                     f"{{file, pattern}} ({type(e).__name__}: {e})")
        out[tag] = {"type": tag, "bootloader_usb": usb, "app_lo": lo, "app_hi": hi, "family": fam,
                    "touch": touch, "dfu": dfu, "drive_serial": drive_serial,
                    "drive_max": int(u.get("drive_max_mb", 64)) * 1024 * 1024,
                    "manual": u.get("manual") or "double-tap its reset button",
                    "refuse_families": refuse, "drive_identity": ident}
    return out


def profile(name):
    """What `name`'s board type says about its UF2 bootloader, or exit saying what is missing."""
    reg = registry_boards().get(name)
    if not reg:
        kit_serial(name)                                   # exits, naming the known boards
    tag = reg["board_tag"]
    if BOARD_TYPE and tag and BOARD_TYPE != tag:
        sys.exit(f"REFUSING: {name} is a {tag!r} in the registry, the caller says {BOARD_TYPE!r}")
    tag = tag or BOARD_TYPE
    types = board_types()
    if not tag:
        sys.exit(f"{name}: its board type is not known: the registry has no policy.board_tag "
                 f"for it; give --type (one of: {', '.join(sorted(types)) or 'none'})")
    if tag not in types:
        sys.exit(f"{name}: board type {tag!r} has no uf2 block in the board catalog "
                 f"({', '.join(BOARDS)}); types with one: {', '.join(sorted(types)) or 'none'}")
    return types[tag]


def find_nrfutil():
    """Locate adafruit-nrfutil on the host this runs on. Checked in order: an explicit
    override, PATH (covers an activated venv), then the default install's venv. A host that
    keeps it elsewhere says so in UF2_NRFUTIL; no home path is hardcoded."""
    if os.environ.get("UF2_NRFUTIL"):
        return os.environ["UF2_NRFUTIL"]
    found = shutil.which("adafruit-nrfutil")
    if found:
        return found
    for candidate in (
        "~/.venv-mcumgr/bin/adafruit-nrfutil",       # the default install's venv
    ):
        p = os.path.expanduser(candidate)
        if os.path.exists(p):
            return p
    sys.exit("uf2_flash: can't find adafruit-nrfutil on this host — install it, "
             "activate its venv, or set UF2_NRFUTIL=/path/to/adafruit-nrfutil")


def usb_identity(dev, sysfs="/sys", cls="block"):
    """(vid, pid, serial) of the USB device a block device (or a partition of it), or with
    cls="tty" a serial port, sits on, read from sysfs; None when it is not on USB."""
    path = os.path.realpath(os.path.join(sysfs, "class", cls, os.path.basename(dev)))
    root = os.path.realpath(sysfs)
    while path not in ("/", root):
        if os.path.exists(os.path.join(path, "idVendor")):
            def rd(name):
                try:
                    return open(os.path.join(path, name)).read().strip()
                except OSError:
                    return ""
            return rd("idVendor").lower(), rd("idProduct").lower(), rd("serial")
        path = os.path.dirname(path)
    return None


def ports_for(serial, prof, by_id="/dev/serial/by-id", sysfs="/sys"):
    """(app_port, boot_port) for a chip serial; either may be None.

    The bootloader is the port whose USB device has one of the board type's bootloader
    VID:PIDs, read from sysfs. "app" is anything else with that serial: the only thing the
    distinction is used for is whether a 1200-baud touch is still needed."""
    app = boot = None
    for p in sorted(glob.glob(os.path.join(by_id, "*"))):
        if serial.lower() not in os.path.basename(p).lower():
            continue
        ident = usb_identity(os.path.realpath(p), sysfs, cls="tty")
        if ident and ident[:2] in prof["bootloader_usb"]:
            boot = p
        else:
            app = p
    return app, boot


def resolve(name):
    serial = kit_serial(name)
    app, boot = ports_for(serial, profile(name))
    if not app and not boot:
        sys.exit(f"{name} ({serial}) is NOT PRESENT on this host")
    return serial, app, boot


def touch_port(port):
    """A 1200-baud touch: open the port at 1200 baud and drop DTR."""
    import serial as pyserial
    try:
        s = pyserial.Serial(port, 1200)
        s.dtr = False
        s.close()
    except Exception as e:
        # The board reboots out from under the ioctl; the touch already landed.
        print(f"  (touch raised {type(e).__name__}: {e} — expected, continuing)")


def do_touch(name):
    prof = profile(name)
    if prof["touch"] != "dfu":
        sys.exit(f"{name}: a {prof['type']} is not put into serial DFU by a 1200-baud touch "
                 f"(its type's uf2.touch is {prof['touch'] or 'not given'})")
    serial_id, app, boot = resolve(name)
    if boot:
        print(f"{name}: already in the bootloader at {boot}")
        return boot, False
    print(f"{name}: 1200-baud touch on {app}")
    touch_port(app)
    # Fast poll: the bootloader appears within ~1 s and then stays. A slow poll can miss
    # the window and make a landed touch look like a failure.
    deadline = time.time() + 20
    while time.time() < deadline:
        _, boot = ports_for(serial_id, profile(name))
        if boot:
            print(f"{name}: bootloader up at {boot}")
            return boot, True
        time.sleep(0.3)
    sys.exit(f"{name}: no bootloader appeared within 20 s — RST double-tap needed?")


def check_uf2(path, prof):
    """Refuse any UF2 that could write outside the board type's app region. A UF2 bootloader
    writes whatever addresses the file names, so this is the only thing between
    a wrong file and an overwritten SoftDevice or bootloader."""
    import struct
    data = open(path, "rb").read()
    if not data or len(data) % 512:
        sys.exit(f"REFUSING: {path} is not a UF2 file (size {len(data)})")
    lo, hi, fams = None, 0, set()
    for i in range(0, len(data), 512):
        m0, m1, flags, addr, size, _bno, _nb, fam = struct.unpack_from("<8I", data, i)
        if m0 != 0x0A324655 or m1 != 0x9E5D5157:
            sys.exit(f"REFUSING: {path} block {i // 512} has no UF2 magic")
        if flags & 0x2000:
            fams.add(fam)
        lo = addr if lo is None else min(lo, addr)
        hi = max(hi, addr + size)
    for fam in fams:
        if fam in prof["refuse_families"]:
            sys.exit(f"REFUSING: {path} is {prof['refuse_families'][fam]} (family 0x{fam:08x})")
    if not fams or not fams <= prof["family"]:
        sys.exit(f"REFUSING: {path} family {[hex(f) for f in fams]} is not a "
                 f"{prof['type']} app family ({', '.join(hex(f) for f in sorted(prof['family']))})")
    if lo < prof["app_lo"] or hi > prof["app_hi"]:
        sys.exit(f"REFUSING: {path} writes 0x{lo:x}..0x{hi:x}, outside the {prof['type']} app "
                 f"region 0x{prof['app_lo']:x}..0x{prof['app_hi']:x}")
    print(f"uf2 ok: {len(data) // 512} blocks, 0x{lo:x}..0x{hi:x}, {prof['type']} app family")


UF2_APPEAR_WAIT = 60.0     # after asking the app for UF2 mode; see the caller


def uf2_disk(serial_id, wait=20.0, missing=None, max_bytes=64 * 1024 * 1024):
    """The bootloader's mass-storage disk, found only through the board's own serial in
    /dev/disk/by-id, never as a bare /dev/sdX, which on a Pi can be the root disk."""
    deadline = time.time() + wait
    while time.time() < deadline:
        hits = [p for p in glob.glob("/dev/disk/by-id/usb-*")
                if serial_id.lower() in os.path.basename(p).lower() and "-part" not in p]
        if len(hits) > 1:
            sys.exit(f"REFUSING: several disks match {serial_id}: {hits}")
        if hits:
            dev = os.path.realpath(hits[0])
            name = os.path.basename(dev)
            try:
                sectors = int(open(f"/sys/block/{name}/size").read())
            except OSError:
                sectors = 0
            if not 0 < sectors * 512 <= max_bytes:
                sys.exit(f"REFUSING: {dev} ({hits[0]}) is {sectors * 512} B -- this type's UF2 "
                         f"bootloader drive is {max_bytes // (1024 * 1024)} MB at most")
            # A desktop automounter (udisks, /media/<label>) may have mounted the drive
            # already. The device was matched by serial, so its existing mount is reused
            # (do_uf2 checks INFO_UF2.TXT there as anywhere else).
            return dev, hits[0]
        time.sleep(0.5)
    sys.exit(missing or f"no UF2 disk for {serial_id} appeared within {wait:.0f} s")


#: The USB vendor a family's shell runs on when the family does not say (Zephyr's).
SHELL_USB_DEFAULT = "2fe3"


def enters():
    """The families' ways of asking an app for UF2 mode the flasher hands over (UF2_ENTERS)."""
    try:
        out = json.loads(os.environ.get("UF2_ENTERS") or "[]")
    except ValueError as e:
        sys.exit(f"UF2_ENTERS is not JSON: {e}")
    return [e for e in out if isinstance(e, dict)]


def on_vendor(entry, ident):
    """Does the entry's shell run on the app's USB ids ((vid, pid, ...))?"""
    wanted = [str(v).strip().lower() for v in (entry.get("shell_usb") or [SHELL_USB_DEFAULT])]
    return bool(ident) and any(v in (ident[0], f"{ident[0]}:{ident[1]}") for v in wanted)


def enter_choice(name, ident, entries, runs=None, exists=os.path.exists):
    """How a running app is asked for UF2 mode: ("shell", family, command), or ("tool",
    family, argv); exits with what to do instead when none may be sent. `ident` is the app's
    USB (vid, pid, ...); `runs`, the family it was read as (or named), only for the message."""
    shell = [e for e in entries if e.get("shell_command") and on_vendor(e, ident)]
    commands = sorted({e["shell_command"] for e in shell})
    if len(commands) == 1:
        fams = [e.get("family") or "?" for e in shell if e["shell_command"] == commands[0]]
        return "shell", "/".join(fams), commands[0]
    if len(commands) > 1:
        usb = f"{ident[0]}:{ident[1]}" if ident else "?"
        each = "; ".join(f"{e.get('family')}: `{e['shell_command']}`" for e in shell)
        sys.exit(f"REFUSING: {name}'s app ({usb}) could be any of {len(shell)} families' "
                 f"firmware, and they ask for UF2 mode differently ({each}). Nothing was sent. "
                 f"Say which it runs: bench_uf2_enter_as: <family> (its host_vars, or -e for "
                 f"one run), or bring the drive up by hand with UF2_ENTER=manual")
    for e in entries:
        for t in e.get("app_tools") or []:
            argv = shlex.split(t)
            if argv and exists(os.path.expanduser(argv[0])):
                return "tool", e.get("family") or "?", argv
    if runs:
        sys.exit(f"{name} runs {runs}, and {runs} says nothing of how to ask its app for UF2 "
                 f"mode (its family's uf2_enter); another family's command is not sent to it. "
                 f"UF2_ENTER=manual waits for it by hand")
    vendors = sorted({v for e in entries for v in (e.get("shell_usb") or [SHELL_USB_DEFAULT])
                      if e.get("shell_command")})
    sys.exit(f"{name} runs an app and nothing was given to ask it for UF2 mode (a family's "
             f"shell on {', '.join(vendors) or 'no vendor'}, or an app tool present here); "
             f"UF2_ENTER=manual waits for it by hand instead")


def say_choice(name, app, choice):
    kind, fam, what = choice
    if kind == "shell":
        return f"{name}: its shell -> `{what}` (UF2 mode, {fam}'s) on {app}"
    return f"{name}: its own tool -> `{' '.join(what)}` (UF2 mode, {fam}'s) on {app}"


def choose(name, app):
    """The choice for the board's app on `app`, from what the flasher handed over."""
    runs = os.environ.get("UF2_RUNS") or os.environ.get("UF2_ENTER_AS")
    return enter_choice(name, usb_identity(os.path.realpath(app), cls="tty"), enters(), runs)


def enter_uf2_mode(name):
    """Bring the board to its bootloader's UF2 mode and return (disk_dev, by_id_link). Resolves
    by the board's serial as always."""
    prof = profile(name)
    if prof["drive_serial"] != "chip":
        sys.exit(f"REFUSING: a {prof['type']}'s UF2 drive does not carry the board's serial, so "
                 f"this tool cannot tell it from another's; flash it with its own flasher "
                 f"(picoboot, for an RP2040 or RP2350)")
    disk = lambda wait, missing: uf2_disk(serial_id, wait=wait, missing=missing,  # noqa: E731
                                          max_bytes=prof["drive_max"])
    serial_id, app, boot = resolve(name)
    if boot and not app:
        # Already in a bootloader. If it is UF2 mode the drive is there; if it is serial-only
        # DFU (an nRF52 after a touch) there is none, and software cannot change that -- it
        # needs a replug (or a double-tap).
        return disk(5.0, f"{name} is in its bootloader with no drive (serial DFU): replug it "
                         f"(it boots its app), then rerun")
    how = os.environ.get("UF2_ENTER", "auto")
    if how not in ("auto", "manual"):
        sys.exit(f"UF2_ENTER={how!r}: auto or manual")
    if how == "manual":
        wait = float(os.environ.get("UF2_MANUAL_WAIT", "90"))
        print(f"{name}: {prof['manual']} now; waiting {wait:.0f} s for its drive", flush=True)
        return disk(wait, f"{name}: no UF2 drive within {wait:.0f} s of asking for it by hand "
                          f"({prof['manual']}); nothing was written")
    if prof["touch"] == "uf2":
        print(f"{name}: 1200-baud touch (UF2 mode) on {app}")
        touch_port(app)
        return disk(UF2_APPEAR_WAIT, f"no UF2 disk for {serial_id} appeared within "
                                     f"{UF2_APPEAR_WAIT:.0f} s of the touch")
    choice = choose(name, app)
    print(say_choice(name, app, choice))
    kind, _fam, what = choice
    if kind == "shell":
        command = what
        import serial as pyserial
        try:
            s = pyserial.Serial(app, 115200, timeout=0.5)
            s.write(b"\r\n"); time.sleep(0.3)
            for ch in command.encode() + b"\r\n":
                s.write(bytes([ch])); time.sleep(0.005)
            time.sleep(0.5)
            s.close()
        except Exception as e:  # the board resets mid-write -- expected
            print(f"  ({type(e).__name__}: {e} -- expected as it resets)")
    else:
        argv = [os.path.expanduser(what[0])] + [a.replace("{port}", app) for a in what[1:]]
        r = subprocess.run(["timeout", "60", *argv], capture_output=True, text=True)
        print("  " + "\n  ".join((r.stdout + r.stderr).strip().splitlines()[-2:]))
    # The drive's by-id link appears only once the kernel has read the device, and a bootloader
    # that leaves its first mass-storage read unanswered holds that for the SCSI command
    # timeout, 30 s (docs/HARDWARE.md, "nRF52840 with a UF2 bootloader"). The wait
    # outlasts it, so such a board is slow, not absent.
    return disk(UF2_APPEAR_WAIT, f"no UF2 disk for {serial_id} appeared within "
                                 f"{UF2_APPEAR_WAIT:.0f} s: if its firmware has no command for "
                                 f"UF2 mode, rerun with UF2_ENTER=manual")


def copy_argv(uf2, mnt):
    """How an image is copied onto the drive: as this user where the mount lets this user
    write, else through sudo."""
    dest = os.path.join(mnt, "NEW.UF2")
    if os.access(mnt, os.W_OK):
        return ["cp", uf2, dest]
    return ["sudo", "-n", "cp", uf2, dest]


def mounted_at(dev):
    r = subprocess.run(["findmnt", "-rno", "TARGET", "-S", dev], capture_output=True, text=True)
    return r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else None


def uf2_mount(dev, serial_id, automount_wait=15.0, run=subprocess.run, sleep=time.sleep,
              clock=time.monotonic):
    """(mountpoint, mounted_by_us). Reuses an existing mount of exactly @p dev (an automount),
    else mounts it under /tmp with passwordless sudo. A host without that is usually a desktop
    that mounts the drive for the user a moment after it appears: that mount is waited for,
    then asked of udisks (a user's mount, no sudo)."""
    mnt = mounted_at(dev)
    if mnt:
        return mnt, False
    mnt = f"/tmp/uf2_flash-{serial_id}"
    os.makedirs(mnt, exist_ok=True)
    r = run(["sudo", "-n", "mount", "-t", "vfat", "-o",
             f"uid={os.getuid()},gid={os.getgid()}", dev, mnt], capture_output=True, text=True)
    if r.returncode == 0:
        return mnt, True
    why = r.stderr.strip()
    end = clock() + automount_wait
    while clock() < end:
        auto = mounted_at(dev)
        if auto:
            return auto, False
        sleep(0.5)
    if shutil.which("udisksctl"):
        u = run(["udisksctl", "mount", "-b", dev, "--no-user-interaction"],
                capture_output=True, text=True)
        auto = mounted_at(dev)
        if auto:
            return auto, False
        why += f"; udisksctl: {(u.stdout + u.stderr).strip()}"
    sys.exit(f"mount {dev} failed: {why} (no passwordless sudo for mount, and no automount)")


# ---- the drive guard ----------------------------------------------------------------------
# Every UF2 drive of one board type looks the same: same label, same INFO_UF2.TXT (model, board
# id, version), same volume serial. Only the USB serial differs, and /dev/disk/by-id is one
# thing that carries it, not proof of it: a name is matched by substring, a /dev/sdX is reused,
# a board can reset between the lookup and the copy. So before anything is written to a drive,
# and again right before the write: ask sysfs which USB device the mounted block device sits on
# (vid, pid, serial), refuse unless it is a bootloader of the board's type with the expected serial,
# and, where the bootloader says it (the type's `drive_identity`), refuse unless the drive's own
# contents name the same chip too. The bootloader's own words are the only identity that cannot
# be a stale lookup.


def mount_source(mnt):
    """The block device a mount point is a mount of."""
    r = subprocess.run(["findmnt", "-no", "SOURCE", "--target", mnt],
                       capture_output=True, text=True)
    return r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else None


def check_drive(name, serial_id, mnt, prof, expect_st_dev=None, sysfs="/sys"):
    """Refuse (SystemExit) unless the drive mounted at @p mnt is `name`'s UF2 bootloader.
    Returns what was established, for the caller to compare again later."""
    src = mount_source(mnt)
    if not src:
        sys.exit(f"REFUSING: {mnt} is not a mount point (the drive went away)")
    ident = usb_identity(src, sysfs)
    if ident is None:
        sys.exit(f"REFUSING: {src} (mounted at {mnt}) is not a USB device")
    vid, pid, usb_serial = ident
    if (vid, pid) not in prof["bootloader_usb"]:
        sys.exit(f"REFUSING: {src} is USB {vid}:{pid}, not a {prof['type']} UF2 bootloader "
                 f"({', '.join(':'.join(x) for x in sorted(prof['bootloader_usb']))})")
    if usb_serial.lower() != serial_id.lower():
        sys.exit(f"REFUSING: the drive at {mnt} ({src}) is chip {usb_serial}, not {name} "
                 f"({serial_id}): nothing was written")
    st_dev = os.stat(mnt).st_dev
    if expect_st_dev is not None and st_dev != expect_st_dev:
        sys.exit(f"REFUSING: the drive at {mnt} is not the one that was checked first "
                 f"(it was re-attached or replaced): nothing was written")
    info = os.path.join(mnt, "INFO_UF2.TXT")
    if not os.path.exists(info):
        sys.exit(f"REFUSING: {src} has no INFO_UF2.TXT -- not a UF2 bootloader drive")
    confirmed_by = "usb-serial"
    ident = prof.get("drive_identity")
    said = os.path.join(mnt, ident["file"]) if ident else None
    if said and os.path.exists(said):
        m = ident["pattern"].search(open(said, errors="replace").read())
        if not m:
            sys.exit(f"REFUSING: {said} does not name a chip")
        if m.group(1).lower() != serial_id.lower():
            sys.exit(f"REFUSING: the drive says it is chip {m.group(1)}, not {name} "
                     f"({serial_id}): nothing was written")
        confirmed_by = ident["file"]
    return {"source": src, "usb": f"{vid}:{pid}", "serial": usb_serial, "st_dev": st_dev,
            "confirmed_by": confirmed_by}


def lock_dir():
    """Where this host's device locks live: devlock's directory (devlock.py `_lockdir`, the
    same rule), so every tool that locks a board looks in one place, and never in /tmp unless
    the host has no runtime dir at all."""
    explicit = os.environ.get("DEVLOCK_DIR")
    if explicit:
        d = os.path.expanduser(explicit)
    else:
        run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        d = os.path.join(run if os.path.isdir(run) else f"/tmp/devlock-{os.getuid()}", "devlock")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def board_lock(serial_id):
    """One run per board at a time (two parallel runs on one board are how a drive gets two
    writers). Non-blocking: a second run is refused, not queued. A tool that drives this one
    for a longer job (a bootloader update) takes the same lock, `uf2-<serial>.lock`, and says
    it holds it in UF2_LOCK_HELD."""
    held = os.environ.get("UF2_LOCK_HELD") or ""
    if held.lower() == serial_id.lower():
        return None
    import fcntl
    fh = open(os.path.join(lock_dir(), f"uf2-{serial_id.lower()}.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"REFUSING: another UF2 or bootloader run is already working on {serial_id}")
    return fh   # held until the process exits


def app_window(data, lo, hi, family=None):
    """The blocks of a UF2 (a drive's CURRENT.UF2: the SoftDevice and the app) that lie inside
    [lo, hi), renumbered as a file of their own. Nothing outside the board type's app region is
    ever written back, whatever the dump holds. `family`: the UF2 family id to carry. A
    bootloader may label its own dump with a board-specific family (one nRF52840 bootloader:
    0x28860045, its USB id); the board type's app family is the one every app image carries,
    which the bootloader takes and the copy's own check (check_uf2) accepts."""
    import struct
    keep = []
    for i in range(0, len(data) - len(data) % 512, 512):
        blk = bytearray(data[i:i + 512])
        m0, m1, _flags, addr, size, _bno, _nb, _fam = struct.unpack_from("<8I", blk, 0)
        if m0 == 0x0A324655 and m1 == 0x9E5D5157 and lo <= addr and addr + size <= hi:
            keep.append(blk)
    for n, blk in enumerate(keep):
        struct.pack_into("<II", blk, 20, n, len(keep))
        if family is not None:
            flags = struct.unpack_from("<I", blk, 8)[0] | 0x2000
            struct.pack_into("<I", blk, 8, flags)
            struct.pack_into("<I", blk, 28, family)
    return b"".join(bytes(b) for b in keep)


def do_app_back(name):
    """A board in its bootloader's UF2 mode back to its app, by copying back the app it holds.
    A board already in its app is left alone."""
    import tempfile
    prof = profile(name)
    serial_id = kit_serial(name)
    app, boot = ports_for(serial_id, prof)
    if app and not boot:
        print(f"{name}: already in its app at {app}")
        return
    dev, link = enter_uf2_mode(name)     # a drive already up is used as it is
    mnt, ours = uf2_mount(dev, serial_id)
    try:
        check_drive(name, serial_id, mnt, prof)
        data = open(os.path.join(mnt, "CURRENT.UF2"), "rb").read()
    finally:
        if ours:
            subprocess.run(["sudo", "-n", "umount", "-l", mnt], capture_output=True)
    back = app_window(data, prof["app_lo"], prof["app_hi"], family=min(prof["family"]))
    if not back:
        sys.exit(f"STOP {name}: its CURRENT.UF2 holds no app inside 0x{prof['app_lo']:x}.."
                 f"0x{prof['app_hi']:x}: left in UF2 mode")
    with tempfile.NamedTemporaryFile(suffix=".uf2", delete=False) as f:
        f.write(back)
    try:
        print(f"{name}: copying its own app back ({len(back) // 512} blocks) to leave UF2 mode")
        do_uf2(name, f.name)
    finally:
        os.unlink(f.name)


def do_uf2(name, uf2):
    prof = profile(name)
    check_uf2(uf2, prof)
    serial_id = kit_serial(name)
    _lock = board_lock(serial_id)  # noqa: F841 (held until exit)
    dev, link = enter_uf2_mode(name)
    print(f"{name}: UF2 drive {link} -> {dev}")
    mnt, ours = uf2_mount(dev, serial_id)
    try:
        first = check_drive(name, serial_id, mnt, prof)
        info = os.path.join(mnt, "INFO_UF2.TXT")
        print(f"{name}: {open(info).readline().strip()}")
        print(f"{name}: drive identity confirmed by {first['confirmed_by']} "
              f"({first['usb']} {first['serial']})")
        print(f"{name}: copying {os.path.basename(uf2)} ({os.path.getsize(uf2)} B)", flush=True)
        try:
            # Again, right before the write: the first check is minutes old by the time a copy
            # starts on a busy host, and this is the last moment a wrong drive can be refused.
            check_drive(name, serial_id, mnt, prof, expect_st_dev=first["st_dev"])
            # A desktop's automount belongs to the user (udisks): a plain copy. Another is
            # often root-owned (drwxr-xr-x root): sudo cp, which needs passwordless sudo.
            subprocess.run(copy_argv(uf2, mnt), check=True)
            os.sync()
        except (OSError, subprocess.CalledProcessError) as e:
            # The bootloader resets as soon as the last block lands, which can
            # yank the disk mid-sync: that is success, not failure. The app
            # port appearing below is the real verdict.
            print(f"  (copy ended with {type(e).__name__}: {e} -- judged by the app below)")
    finally:
        if ours:
            subprocess.run(["sudo", "-n", "umount", "-l", mnt], capture_output=True)
    deadline = time.time() + 45
    while time.time() < deadline:
        app, boot = ports_for(serial_id, prof)
        if app and not boot:
            print(f"{name}: programmed; app up at {app}")
            return
        time.sleep(0.5)
    app, boot = ports_for(serial_id, prof)
    sys.exit(f"{name}: no app port within 45 s (bootloader {'still present' if boot else 'gone'})"
             f" -- if it is gone the board may be in its bootloader's BLE DFU mode")


def main():
    global BOARD_TYPE
    if len(sys.argv) > 2 and sys.argv[1] == "--type":
        BOARD_TYPE = sys.argv[2]
        del sys.argv[1:3]
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "list":
        names, types = {}, board_types()
        for n, e in registry_boards().items():      # one row per board, its aliases beside it
            names.setdefault(e["serial"], []).append(n)
        for s, ns in names.items():
            canon = ns[0]                     # the registry key; aliases follow it
            tag = registry_boards()[canon]["board_tag"]   # the registry's word only: --type is one board's
            if tag in types:
                app, boot = ports_for(s, types[tag])
                state = "APP" if app else ("BOOTLOADER" if boot else "absent")
            else:                             # no type: present or not is all that can be said
                app = next((p for p in sorted(glob.glob("/dev/serial/by-id/*"))
                            if s.lower() in os.path.basename(p).lower()), None)
                boot, state = None, "PRESENT" if app else "absent"
            also = [n for n in ns if n != canon]
            print(f"{canon:6} {s}  {tag or '(no type)':12} {state:11} {app or boot or ''}"
                  + (f"  (also: {', '.join(also)})" if also else ""))
        return
    if cmd == "resolve":
        s, app, boot = resolve(sys.argv[2])
        print(app or boot)
        return
    # A board whose console the rxrbench serial broker owns is not flashed from here: the
    # broker holds the port. Flash it with rxrbench, or stop its broker first.
    if cmd in ("touch", "uf2-mode", "uf2", "dfu", "app-back") and len(sys.argv) > 2:
        sock = f"/run/user/{os.getuid()}/rxrbench/{sys.argv[2]}.sock"
        # rxrbench itself flashes through here, after its broker has released the console:
        # it says so by naming the board in RXRBENCH_RELEASED.
        if os.path.exists(sock) and os.environ.get("RXRBENCH_RELEASED") != sys.argv[2]:
            sys.exit(f"REFUSING: {sys.argv[2]}'s console is owned by the rxrbench broker "
                     f"(systemctl --user stop rxrbench-serial@{sys.argv[2]} to release it)")
    if cmd == "touch":
        do_touch(sys.argv[2])[0]
        return
    if cmd == "verify-drive":
        # verify-drive <name> <mountpoint>: the drive guard on its own (a bootloader update
        # calls it before it reads or writes a drive it mounted a while ago).
        g = check_drive(sys.argv[2], kit_serial(sys.argv[2]), sys.argv[3], profile(sys.argv[2]))
        print(json.dumps(g, default=str))
        return
    if cmd == "enter-choice" and len(sys.argv) > 2:
        name = sys.argv[2]
        prof = profile(name)
        _serial, app, boot = resolve(name)
        if boot and not app:
            print(f"{name}: UF2 ENTER: already in its bootloader")
        elif os.environ.get("UF2_ENTER", "auto") == "manual":
            print(f"{name}: UF2 ENTER: by hand ({prof['manual']})")
        elif prof["touch"] == "uf2":
            print(f"{name}: UF2 ENTER: 1200-baud touch on {app}")
        else:
            print(f"{name}: UF2 ENTER: " + say_choice(name, app, choose(name, app))
                  .split(": ", 1)[1])
        return
    if cmd == "uf2-mode":
        _lock = board_lock(kit_serial(sys.argv[2]))  # noqa: F841
        dev, link = enter_uf2_mode(sys.argv[2])
        mnt, ours = uf2_mount(dev, kit_serial(sys.argv[2]))
        print(f"{sys.argv[2]}: UF2 drive {link} -> {dev} mounted at {mnt}")
        g = check_drive(sys.argv[2], kit_serial(sys.argv[2]), mnt, profile(sys.argv[2]))
        print(f"{sys.argv[2]}: drive identity confirmed by {g['confirmed_by']} "
              f"({g['usb']} {g['serial']})")
        info = os.path.join(mnt, "INFO_UF2.TXT")
        if os.path.exists(info):
            print(open(info).read().rstrip())
        print("(left in UF2 mode; it returns to its app on reset or after a UF2 copy)")
        return
    if cmd == "uf2":
        do_uf2(sys.argv[2], sys.argv[3])
        return
    if cmd == "app-back":
        do_app_back(sys.argv[2])
        return
    if cmd == "dfu":
        name, pkg = sys.argv[2], sys.argv[3]
        if not os.path.exists(pkg):
            sys.exit(f"no such package: {pkg}")
        if profile(name)["dfu"] != "adafruit-nrfutil":
            sys.exit(f"{name}: its type names no serial DFU tool (uf2.dfu: adafruit-nrfutil)")
        boot, touched = do_touch(name)
        # Re-resolve immediately before writing: this is the guard step, and it
        # is the reason the port is never passed in from outside.
        serial_id = kit_serial(name)
        if serial_id.lower() not in os.path.basename(boot).lower():
            sys.exit(f"REFUSING: {boot} is not {name} ({serial_id})")
        # The first DFU session against a freshly entered bootloader fails and adafruit-nrfutil
        # exits 0 anyway (docs/HARDWARE.md, "nRF52840 with a UF2 bootloader"). Success
        # is judged by "Device programmed." in its output and the bootloader leaving the bus;
        # a retry is one more session, never a re-touch: the board is already in the
        # bootloader.
        for attempt in (1, 2):
            print(f"{name}: flashing {os.path.basename(pkg)} -> {boot}"
                  + (" (second session)" if attempt == 2 else ""), flush=True)
            r = subprocess.run([find_nrfutil(), "dfu", "serial", "--package", pkg,
                                "-p", boot, "-b", "115200", "--singlebank"],
                               capture_output=True, text=True)
            out = (r.stdout or "") + (r.stderr or "")
            tail = [l for l in out.splitlines() if l.strip() and not l.startswith("#")]
            print("\n".join(tail[-4:]), flush=True)
            programmed = "Device programmed" in out
            if programmed:
                deadline = time.time() + 15
                while time.time() < deadline:
                    _, still = ports_for(serial_id, profile(name))
                    if not still:
                        print(f"{name}: programmed; bootloader gone, app coming up", flush=True)
                        sys.exit(0)
                    time.sleep(0.5)
                sys.exit(f"{name}: nrfutil said programmed but the bootloader is still "
                         f"on the bus after 15 s -- check it by hand")
            _, still = ports_for(serial_id, profile(name))
            if not still:
                sys.exit(f"{name}: DFU session {attempt} failed and the bootloader is gone "
                         f"-- the board reset on its own; re-run to touch it again")
            if attempt == 2:
                break
            print(f"{name}: DFU session 1 failed with the bootloader still present "
                  "(the known first-session EIO); opening a second session", flush=True)
            time.sleep(1.0)
        sys.exit(f"{name}: both DFU sessions failed; bootloader still at {boot}")
    sys.exit(f"unknown command '{cmd}'")


if __name__ == "__main__":
    main()
