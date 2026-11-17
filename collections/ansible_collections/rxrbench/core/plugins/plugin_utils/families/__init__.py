"""What kind of thing a device is, as data.

A family is the firmware (or operating system) a device runs. Everything that differs between
families is a field of its row, and code asks this table instead of comparing a family's name:
a new family is a new row, not a new `if`. The families here need no plugin; a plugin
collection brings more (registry.py), and the table is these and theirs together.

    vocabulary     the configuration language it speaks, or None (nothing this tool
                   configures). It names the task file of its plugin's `board` role that brings
                   a board's config to its declared state, and the facts task beside it.
    state_command  the console command that prints its state line (`~S{json}*crc16`), or None
                   when it has none
    identity_key   the field, beside build and version, that says which image it is (a class,
                   a role), or None
    build          catalog  images are built here from a recipe in the family's own catalog
                   images   images are listed, prebuilt, in the site's firmware catalog
    config_via     how its configuration is written (its plugin's word for it), or None
    radio          managed    its radio can be set here, so the RF rules are enforced
                   unmanaged  it transmits at its own firmware's defaults: allowed, warned about
                   none       it has no radio the RF rules are about
    update         how it is brought to its declared image: None, by one of this tool's own
                   flashers (the `board` role: esptool, UF2, MCUmgr, a debug probe), then its
                   config; or its plugin's word, naming the update task of the plugin's
                   `board` role (`update_<word>.yml`), which does the whole of it
    console        the kind of console it has: zephyr-shell (echo, then the shell's prompt; read
                   to a marker), a line dialect its plugin gives the broker, or None
    locate         how a device is found on the bench: its plugin's word for the indicator it
                   has (an LED, a light, a buzzer), naming the locate task of the plugin's
                   `board` role, or None
    plugin         the Family object that answers for it: everything a family knows that is
                   not a field here is a hook on that class (base.py lists them), so generic
                   code asks `plugin(family)` and never names a family

This package runs on the controller only. A module on a bench host is handed the families as
data (`specs()`, module_utils/familyspec.py).
"""
from . import bare, base, foreign, zephyr_mcumgr
from .. import registry
from ...module_utils import hwsurvey, regions, routes, site
from ...module_utils.flash import common as flash_common

_NOTHING = {"vocabulary": None, "state_command": None, "identity_key": None, "build": "images",
            "config_via": None, "radio": "none", "update": None, "console": None,
            "locate": None}
CORE_FAMILIES = {
    # Any Zephyr application with MCUmgr: updated over SMP, known by its image hash.
    "zephyr_mcumgr": {**_NOTHING, "plugin": "zephyr_mcumgr"},
    # Firmware with no console and no update protocol of its own (a bare sample, a vendor
    # binary): written whole through a debug probe, known only by its bytes.
    "bare": {**_NOTHING, "plugin": "bare"},
}
# A family this table does not know is foreign. Nothing is known of what it does, so it is
# taken to have a radio this tool does not set: allowed, warned about, and counted as
# transmitting. The cautious answer is the default on purpose.
FOREIGN = {**_NOTHING, "radio": "unmanaged", "plugin": "foreign"}
CORE_PLUGINS = {m.__name__.rsplit(".", 1)[-1]: m.Family() for m in
                (bare, foreign, zephyr_mcumgr)}

_table = None


def load(names=None, roots=()):
    """Build the table from the core families and the plugin collections `names` (default:
    the site's, site.plugins()). Called on first use; call it again to change what is loaded."""
    global _table  # pylint: disable=global-statement
    rows, plugins, owners, usb = dict(CORE_FAMILIES), dict(CORE_PLUGINS), {}, {}
    flashers, recognisers, bearers_of, deliveries = {}, [], {}, {}
    for collection, mod in registry.load(site.plugins() if names is None else names, roots).items():
        theirs = getattr(mod, "PLUGINS", {})
        # the USB ids of the boards its devices are (hwsurvey.KINDS says what a row is)
        usb.update(getattr(mod, "USB_KINDS", {}))
        # what an image looks like at the start of a region (module_utils/regions.py)
        recognisers.extend(getattr(mod, "RECOGNISERS", ()))
        # what a device written by a flasher the plugin brings can have done to it beside its
        # image being written (the levels of flash/common.py CAPABILITIES: regions, chip)
        # the bearers each of its flashers writes over (a probe flasher: swd)
        for flasher, bearers in (getattr(mod, "FLASHER_BEARERS", None) or {}).items():
            bearers_of[flasher] = tuple(bearers)
        # the bearers it brings that carry an image to the device themselves, each with the
        # tool that does it on a bench host and the image formats it takes (ROUTE_DELIVERIES)
        for bearer, how in (getattr(mod, "ROUTE_DELIVERIES", None) or {}).items():
            if bearer in deliveries or bearer in routes.DEFAULT_ORDER:
                raise registry.PluginError(f"{collection}: a route named {bearer!r} is already known")
            if not how.get("tool") or not how.get("formats"):
                raise registry.PluginError(f"{collection}: the delivering route {bearer!r} names no "
                                           "tool or no image formats")
            deliveries[bearer] = {"bearer": bearer, "tool": how["tool"],
                                  "formats": list(how["formats"]),
                                  "settings": how.get("settings"),
                                  "credential": how.get("credential")}
        for flasher, caps in (getattr(mod, "FLASHERS", None) or {}).items():
            if flasher in flash_common.FLASHERS or flasher in flashers:
                raise registry.PluginError(f"{collection}: a flasher named {flasher!r} is already known")
            flashers[flasher] = tuple(caps)
        for key, obj in theirs.items():
            if key in plugins:
                raise registry.PluginError(f"{collection}: a family plugin named {key!r} is already loaded")
            plugins[key] = obj
        for family, row in mod.FAMILIES.items():
            if family in rows:
                raise registry.PluginError(f"{collection}: family {family!r} is one of the built-in families")
            row = {**_NOTHING, **row}
            if row.get("plugin") not in plugins:
                # a family with a row and nothing more to say
                plugins[f"{collection}:{family}"] = base.Family()
                row["plugin"] = f"{collection}:{family}"
            rows[family], owners[family] = row, collection
    _table = {"FAMILIES": rows, "PLUGINS": plugins, "OWNERS": owners, "USB_KINDS": usb,
              "FLASHERS": flashers, "RECOGNISERS": recognisers, "BEARERS": bearers_of,
              "DELIVERIES": deliveries}
    hwsurvey.register_kinds(usb)
    regions.register_recognisers(recognisers)
    return _table


def flasher_can(flasher):
    """What a device written by `flasher` can have done to it: core's own flashers
    (flash/common.py CAPABILITIES), then those the loaded plugins bring (their FLASHERS)."""
    return flash_common.can(flasher) or list((_table or load())["FLASHERS"].get(flasher) or ())


def flasher_bearers(flasher):
    """The bearers a flasher writes over: core's (flash/common.py BEARERS), then a loaded
    plugin's (its FLASHER_BEARERS); None when nothing declares them."""
    if flasher in flash_common.BEARERS:
        return flash_common.BEARERS[flasher]
    return (_table or load())["BEARERS"].get(flasher)


def route_delivery(bearer):
    """How a route delivers an image itself, or None: a loaded plugin's ROUTE_DELIVERIES row
    for that bearer ({bearer, tool, formats, settings, credential}). A device whose flash route is such a
    bearer is not written by the bench host: the route carries the image, and the family's
    own flasher still reads the device before and after."""
    how = (_table or load())["DELIVERIES"].get(bearer)
    return dict(how) if how else None


def delivering():
    """The bearers that carry an image to the device themselves (the loaded plugins')."""
    return tuple((_table or load())["DELIVERIES"])


def takes_delivery(family, flasher, formats):
    """The formats among `formats` a device can take from a delivering route: those its
    family's own update step takes (Family.takes_delivery), and the one a core flasher that
    writes its image hands over (flash/common.py DELIVERS)."""
    own = set(plugin(family).takes_delivery) | {flash_common.DELIVERS.get(flasher)}
    return [f for f in formats if f in own]


def flashers():
    """Every flasher a catalogued image may name: core's own, then those the loaded plugins
    declare (their FLASHERS)."""
    return set(flash_common.FLASHERS) | set((_table or load())["FLASHERS"])


def usb_kinds():
    """The loaded plugins' USB ids, as data for a module on a bench host."""
    return dict((_table or load())["USB_KINDS"])


def __getattr__(name):
    """FAMILIES, PLUGINS and OWNERS (family -> the collection that brings it) are the loaded
    table's."""
    if name in ("FAMILIES", "PLUGINS", "OWNERS"):
        return (_table or load())[name]
    raise AttributeError(name)


def firmware(hostvars):
    """What a device is declared to run: its top-level `firmware:` block."""
    return hostvars.get("firmware") or {}


def declared(hostvars):
    """The family a device declares (`firmware.family`), or None."""
    return firmware(hostvars).get("family")


def known(family):
    """Does a loaded plugin, core or the catalog bring this family?"""
    return family in (_table or load())["FAMILIES"]


def of(family):
    return (_table or load())["FAMILIES"].get(family, FOREIGN)


def owner(family):
    """The plugin collection that brings a family, or None for a built-in or unknown one."""
    return (_table or load())["OWNERS"].get(family)


def radio(family):
    return of(family)["radio"]


def built_from_catalog(family):
    """Are this family's images built here from a recipe (rather than listed)?"""
    return of(family)["build"] == "catalog"


def plugin(family):
    """The Family object that answers for a family (FOREIGN's for one the table does not know)."""
    return (_table or load())["PLUGINS"][of(family)["plugin"]]


def specs():
    """{family: its spec} for every family: the table a module on a bench host is handed
    (familyspec.py), since it cannot import a plugin."""
    return {name: plugin(name).spec(row) for name, row in (_table or load())["FAMILIES"].items()}


def survey_tags(probes, boards=None):
    """The tags every family gives a surveyed board, from its probe of it (`probes` is keyed
    by family)."""
    out = set()
    for fam in (_table or load())["FAMILIES"]:
        if probes.get(fam):
            out |= plugin(fam).survey_tags(probes[fam], boards)
    return out
