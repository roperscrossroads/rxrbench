"""A plugin with four families and one delivering route (README.md): a widget that answers `widget state` with a state
line; a gadget, an ESP32 release written whole; a beacon, whose LoRa radio this tool sets; and
a probe chip, a family whose devices have no console and are written through a debug probe by
this plugin's own flasher (`probeflash`), from images built elsewhere. The last is the
smallest example of a probe-flashed family (docs/ARCHITECTURE.md#core-portability)."""
from ansible_collections.rxrbench.core.plugins.module_utils import rfrules
from ansible_collections.rxrbench.core.plugins.plugin_utils.families import base

FAMILIES = {
    "widget": {"state_command": "widget state", "console": "zephyr-shell", "identity_key": "class",
               "plugin": "widget"},
    "gadget": {"vocabulary": None, "state_command": None, "identity_key": None, "build": "images",
               "config_via": None, "radio": "none", "update": None, "console": None,
               "locate": None, "plugin": "gadget"},
    "beacon": {"vocabulary": None, "state_command": None, "identity_key": None, "build": "images",
               "config_via": None, "radio": "managed", "update": None, "console": None,
               "locate": None, "plugin": "beacon"},
    "probechip": {"vocabulary": None, "state_command": None, "identity_key": None,
                  "build": "images", "config_via": None, "radio": "none", "update": "probe",
                  "console": None, "locate": None, "plugin": "probechip"},
}


class Widget(base.Family):
    """A board on a Zephyr shell that says what it runs in a state line: the generic identity
    mechanisms core's flashers and plays compare by, with nothing of any real firmware. A board
    is declared as a class with features; the image's class is also in its bytes."""
    state = {"version": 1, "strip_build": True,
             "fields": {"target": "board", "version": "image", "build": "build", "class": "class",
                        "features": "feat"}}
    image_match = ("class", "features")
    image_bytes_check = {"field": "class", "args": ["--class"]}
    features = ("fast", "quiet")
    #: a widget image may also reach the board by a route that carries it (ROUTE_DELIVERIES)
    takes_delivery = ("widget-image",)
    #: what `just diag` asks a widget when its layout does not say
    diag_commands = ({"name": "version", "cmd": "widget version"},
                     {"name": "state", "cmd": "widget state"})
    #: an ESP32 image's config regions: the first is essential, the second optional
    config_regions = (("storage", "STORAGE"), ("bulk", "BULK"))
    #: its shell asks the bootloader for UF2 mode; a first flash needs a bootloader whose
    #: version carries this word
    uf2_enter = {"shell_command": "widget dfu", "shell_usb": ["f1d0"], "bootloader": "OTAFIX"}

    def survey_tags(self, probe, boards=None):
        """What its state line says: the firmware, its class, and the board's MCU when a board
        type lists the Zephyr board it names."""
        t = {"fw:widget"}
        board = probe.get("board") or ""
        for kind in (boards or {}).values():
            if board in (kind.get("zephyr_boards") or ()) and kind.get("mcu"):
                t.add(f"mcu:{kind['mcu']}")
        if probe.get("class") is not None:
            t.add(f"class:{probe['class']}")
        return t

    def survey_describe(self, probe):
        # found by its probe, maybe with nothing more known
        return f"widget class {probe.get('class', '?')}" + (
            f" on {probe['board']}" if probe.get("board") else "")


class Gadget(base.Family):
    """An ESP32 release written whole (a merged image) with no console of its own: what it runs
    is the image's hash, and its config regions are the data partitions its own partition table
    names, read, written back and erased through the ROM loader."""
    config_regions = (("fs", "SPIFFS"), ("nvs", "NVS"))
    region_flashers = ("esp-image",)

    def survey_tags(self, probe, boards=None):
        """A stock release says its model and version over its own API."""
        return {"fw:gadget"} | ({f"hw:{probe['model']}"} if probe.get("model") else set())

    def survey_describe(self, probe):
        return f"gadget {probe['version']}" if probe.get("version") else ""

    def enroll_note(self, probe):
        """What a board found running a release says of itself, kept in its registry entry."""
        said = {k: probe[k] for k in ("model", "version", "name") if probe.get(k)}
        return {**({"model": probe["model"]} if probe.get("model") else {}),
                **({"delivered": f"gadget {probe['version']}"} if probe.get("version") else {}),
                **({"alias": probe["name"]} if probe.get("name") else {}),
                **({"identity": said} if said else {})}


class Beacon(base.Family):
    """A board with a LoRa radio this tool sets (radio `managed`): its layout declares the radio
    as `beacon.radio` {tx, tx_dbm, freq_mhz, bw_khz}, and core judges what it would emit
    against the site's RF rules. A board that declares none is a problem: left alone, it
    transmits at whatever its image does."""

    def rf_declared(self, name, hostvars, managed):
        radio = (hostvars.get("beacon") or {}).get("radio")
        if radio is None:
            return {"transmits": None, "emissions": [], "problems": [
                f"{name}: no beacon.radio declared: its radio is unmanaged"] if managed else []}
        if not radio.get("tx", True):
            return {"transmits": False, "emissions": [], "problems": []}
        return {"transmits": True, "problems": [], "emissions": [{
            "radio": "lora", "key": "beacon.radio.tx_dbm", "power_dbm": radio.get("tx_dbm"),
            "freq_mhz": radio.get("freq_mhz"), "bw_khz": radio.get("bw_khz")}]}

    def rf_check(self, name, hostvars, rules):
        """A beacon commands a relay to repeat it (`beacon.relay.tx_dbm`): what its block makes
        another device transmit is judged by the same rules as its own radio."""
        relay = (hostvars.get("beacon") or {}).get("relay")
        if not relay:
            return []
        return rfrules.power(name, relay.get("tx_dbm"), rules, "beacon.relay.tx_dbm", "lora")


class ProbeChip(base.Family):
    """No console: commands go through the probe (step tasks); the chip is read, written back
    and erased by this plugin's task files, since core has no module for its flasher."""
    step_tasks = "step_widget"
    chip_tasks = {"probeflash": {"backup": "chip_backup_probe", "restore": "chip_restore_probe",
                                 "erase": "erase_probe", "region_backup": "region_backup_probe",
                                 "region_restore": "region_restore_probe",
                                 "region_erase": "region_erase_probe"}}
    #: the survey asks the chips behind a debug probe with this plugin's tool (host/bin)
    survey = {"kinds": [], "probes": ["swd-probe"], "tool": "probe_probechip.py"}
    #: its 1 MiB flash as the linker script lays it out: a 16 KiB bootloader, the app, and the
    #: its config in the last 32 KiB (docs/ARCHITECTURE.md#partitions-and-the-write-rule)
    flash_regions = (
        {"label": "bootloader", "kind": "boot", "start": "0x08000000", "size": "0x4000"},
        {"label": "app", "kind": "app", "start": "0x08004000", "size": "0xF4000"},
        {"label": "config", "kind": "data", "start": "0x080F8000", "size": "0x8000",
         "role": "config"},
    )


#: the USB ids of a widget board: its UF2 bootloader and the app it ships with, both giving
#: the chip id as the USB serial (core's hwsurvey.KINDS says what a row is)
USB_KINDS = {
    "f1d1:0001": {"kind": "widget-board", "mcu": "wm1", "mode": "bootloader",
                  "serial_is": "chip_id", "note": "a widget board's UF2 bootloader"},
    "f1d1:0002": {"kind": "widget-board", "mcu": "wm1", "mode": "stock-app",
                  "serial_is": "chip_id", "note": "the app a widget board ships with"},
}

PLUGINS = {"widget": Widget(), "gadget": Gadget(), "beacon": Beacon(), "probechip": ProbeChip()}
#: what a widget image looks like at the start of a region: core's write guard then sees one
#: there (module_utils/regions.py); `WIDG` at offset 0
RECOGNISERS = [{"name": "widget-image", "at": 0, "magic": "57494447"}]
#: the flasher this plugin brings: a whole chip it can read and write back, through a probe,
#: and the config regions declared for it
FLASHERS = {"probeflash": ("regions", "chip")}
FLASHER_BEARERS = {"probeflash": ["swd"]}
#: a route this plugin brings that carries an image to the device itself: the smallest example
#: of one (docs/PLUGINS.md, "A route that delivers"). A widget takes it; no other family does.
ROUTE_DELIVERIES = {"courier": {"tool": "courier_route.py", "formats": ("widget-image",),
                                "settings": "bench_courier", "credential": "bench_courier_key"}}
