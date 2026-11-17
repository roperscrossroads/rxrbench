"""A layout: one directory that declares a whole bench.

    layouts/<name>/
      devreg.yml              which registry boards are in this layout, on which bench host, and
                              (optionally) the layers it inherits and each board's profile
      group_vars/all.yml      what every board of the layout shares (a build, a store)
      host_vars/<board>.yml   one board's declared state (or host_vars/<board>/, several files)

The inventory plugin uses this module to turn a layout plus the device registry into hosts and
groups; the unit tests use it to check every layout without Ansible. A board's declared state
is what Ansible will see: the layers' vars, its profile, then the layout's own files
(module_utils/layers.py composes them). Only plain YAML is read here (a `*.sops.yaml` is
Ansible's to decrypt).
"""
import os
import re

import yaml

from . import layers, location, regions, rflayout, rfrules, routes, site
from ..plugin_utils import families, registry


class LayoutError(ValueError):
    pass



def chip_mac(entry):
    """An ESP32's MAC as the registry holds it: `policy.chip_mac`, else a `usb_serial` (of the
    match or a match_also face) that is a MAC (12 hex, colons or not), else the MAC a
    `serial_by_id` carries (an ESP32's USB-Serial-JTAG names its port by it, as the plug watcher
    finds the board: location.identity). None when none is."""
    mac = (entry.get("policy") or {}).get("chip_mac")
    if mac:
        return str(mac).lower()
    faces = [entry.get("match") or {}] + list(entry.get("match_also") or [])
    for m in faces:
        ser = str((m or {}).get("usb_serial") or "")
        bare = ser.lower().replace(":", "")
        if re.fullmatch(r"[0-9a-f]{12}", bare):
            return ":".join(bare[i:i + 2] for i in range(0, 12, 2))
    for m in faces:
        by_id = str((m or {}).get("serial_by_id") or "")
        found = location.MAC.search(by_id)
        if found:
            return found.group(0).lower()
        bare = re.search(r"_([0-9A-Fa-f]{12})-if", by_id)
        if bare:
            b = bare.group(1).lower()
            return ":".join(b[i:i + 2] for i in range(0, 12, 2))
    return None


def via_of(chosen):
    """How a command reaches a device, from the route chosen for its shell: `console` (a
    serial console, owned by its broker), `ssh`, or None when it has no shell."""
    bearer = (chosen.get("shell") or {}).get("bearer")
    return {"usb": "console", "ssh": "ssh"}.get(bearer)

def default_registry_path():
    return site.registry_path()


def load_registry(path=None):
    path = os.path.expanduser(path or default_registry_path())
    with open(path) as f:
        reg = yaml.safe_load(f) or {}
    if "devices" not in reg:
        raise LayoutError(f"{path}: not a device registry (no 'devices')")
    return reg


def canonical(reg, name):
    """The registry key for a board name or alias, or None."""
    devs = reg["devices"]
    if name in devs:
        return name
    hits = [k for k, e in devs.items() if name in ((e or {}).get("aliases") or [])]
    if len(hits) > 1:
        raise LayoutError(f"{name!r} is an alias of more than one registry device: {hits}")
    return hits[0] if hits else None


def host_connection(reg, host):
    """Ansible connection vars for a bench host, from the registry's hosts table."""
    h = (reg.get("hosts") or {}).get(host)
    if h is None:
        raise LayoutError(f"bench host {host!r} is not in the registry's hosts")
    if h.get("local"):
        return {"ansible_connection": "local"}
    ssh = h.get("ssh")
    if not ssh:
        raise LayoutError(f"bench host {host!r} has no ssh route in the registry")
    user, _, addr = ssh.rpartition("@")
    out = {"ansible_host": addr}
    if user:
        out["ansible_user"] = user
    # A host behind a forwarded port with a key of its own (a simulated bench host in a VM).
    if h.get("port"):
        out["ansible_port"] = int(h["port"])
    if h.get("ssh_key"):
        out["ansible_ssh_private_key_file"] = os.path.expanduser(str(h["ssh_key"]))
    if h.get("known_hosts"):
        out["ansible_ssh_common_args"] = (f"-o UserKnownHostsFile={os.path.expanduser(str(h['known_hosts']))}"
                                          " -o StrictHostKeyChecking=yes")
    return out


def read_host_vars(layout_dir, board):
    """A board's declared state: host_vars/<board>.yml, or the directory host_vars/<board>/ as
    Ansible reads it (its plain YAML files in name order, a later file's top-level keys
    replacing an earlier one's). A layout that shares a board with another links the shared
    declaration into such a directory and adds only what differs (a route, a flasher)."""
    for ext in (".yml", ".yaml"):
        p = os.path.join(layout_dir, "host_vars", board + ext)
        if os.path.exists(p):
            with open(p) as f:
                return yaml.safe_load(f) or {}
    d = os.path.join(layout_dir, "host_vars", board)
    out = {}
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if name.endswith((".yml", ".yaml")) and not name.endswith((".sops.yml", ".sops.yaml")):
                with open(os.path.join(d, name)) as f:
                    out.update(yaml.safe_load(f) or {})
    return out


def load_catalog(*catalog_dirs):
    """{"images", "sources", "boards", "families": {family: its own catalog}} from catalog
    directories, later ones layered over earlier ones (the tool's catalog, then the site's).
    Listed images and source trees are firmware.yml; board types (what a registry board_tag
    is: its MCU, its PSRAM) are boards.yml; a family that builds its images here keeps its
    recipes in a file of its own, read by its plugin (families/<f>.py)."""
    out = {"images": {}, "sources": {}, "boards": {}, "families": {}}
    for catalog_dir in catalog_dirs:
        for fname, keys in (("firmware.yml", ("images", "sources")), ("boards.yml", ("boards",))):
            p = os.path.join(catalog_dir, fname)
            if os.path.exists(p):
                with open(p) as f:
                    doc = yaml.safe_load(f) or {}
                for key in keys:
                    out[key].update(doc.get(key) or {})
    for fam in families.FAMILIES:
        out["families"][fam] = families.plugin(fam).catalog(catalog_dirs)
    return out


def check_firmware(name, hostvars, catalog, board_tag):
    """Is the firmware a board declares something the catalog can produce for that board?
    Returns (problems, record): record is what the family's catalog says of the image
    (board_tags, flasher), or None."""
    fw = families.firmware(hostvars)
    if not fw:
        return [], None
    fam = families.declared(hostvars)
    if fam is None:
        return [f"{name}: firmware declares no family (firmware.family, a family a plugin registers)"], None
    record, problems = families.plugin(fam).image(fw, catalog)
    out = [f"{name}: {p}" for p in problems]
    if record is None:
        return out, None
    # The registry's board type is authoritative where it states one.
    tags = record.get("board_tags") or []
    if board_tag and board_tag not in tags:
        out.append(f"{name}: is a {board_tag}; its declared firmware is for {', '.join(map(str, tags))}")
    return out, record


def groups_for(hostvars, fw_kind):
    """Group names a board belongs to, derived from its declared state: the generic ones
    (what it is, which family, which image, which features), then the family's own."""
    g = ["boards"]
    fw = families.firmware(hostvars)
    fam = families.declared(hostvars)
    plugin = families.plugin(fam)
    if fw_kind == "stock":
        g.append("stock")
    if fam:
        g.append(f"family_{fam}")
    key = families.of(fam)["identity_key"]
    if key and key in fw:
        g.append(f"{key}_{fw[key]}")
    for f in fw.get("features") or []:
        g.append(plugin.feature_groups.get(f, f"feature_{f}"))
    g += [x for x in plugin.groups(hostvars) if x not in g]
    return g


def probe_flasher(own, bearers):
    """(flasher, problem) for a board whose flash route is a debug probe (swd): its image's
    flasher `own` when that one writes over swd (`bearers`, what it declares), core's swd
    flasher when the image names none; a problem when it names one that writes over another
    bearer, or one that declares none."""
    if not own:
        return "swd", None
    if bearers is None:
        return None, (f"its flash route is a debug probe (swd), and its image's flasher {own!r} "
                      "does not say which bearers it writes over (a plugin declares them in "
                      "FLASHER_BEARERS, docs/PLUGINS.md)")
    if "swd" not in bearers:
        return None, (f"its flash route is a debug probe (swd), but its image's flasher {own!r} "
                      f"writes over {', '.join(bearers)}")
    return own, None


def load_layout(layout_dir, config, reg=None):
    """Resolve a layout against the registry.

    config: the parsed devreg.yml ({boards: {name: {host: ...}}, rf_rules, registry, catalog_dir}).
    Returns {"bench_hosts": {host: conn_vars}, "boards": {name: {...}}, "warnings": [...]};
    raises LayoutError
    listing every problem at once.
    """
    # A relative path in devreg.yml is relative to the layout: a layout may bring a registry
    # and a catalog of its own (the simulated bench does).
    def local(path):
        path = os.path.expanduser(path) if path else path
        return os.path.join(layout_dir, path) if path and not os.path.isabs(path) else path

    reg = reg or load_registry(local(config.get("registry")))
    own = local(config.get("catalog_dir"))
    shared = registry.catalog_dirs()
    catalog = load_catalog(own) if own else load_catalog(*shared)
    # Board types are not a bench's: the tool's table, the plugins' and the site's hold under a
    # layout's own catalog too (which may add types of its own).
    catalog["boards"] = load_catalog(*shared, *([own] if own else []))["boards"]
    # A layout that is another with a few boards changed (`from: <layout>`): the rest of that
    # layout's boards come along (module_utils/layers.py, expand).
    try:
        config = layers.expand(layout_dir, config)
    except layers.LayerError as e:
        raise LayoutError(str(e)) from e
    boards = config.get("boards") or {}
    if not boards:
        raise LayoutError(f"{layout_dir}: devreg.yml lists no boards")
    problems, out_boards, bench_hosts, all_hv, warnings = [], {}, {}, {}, []
    seen_index = location.seen(config.get("location_dir"))
    # A bench host that is this machine is read live, over what the relay last said of it.
    try:
        seen_index.update(location.seen_here(site.local_hosts()))
    except (OSError, ValueError):
        pass             # no `hosts` layout (a simulated bench): the relay's index alone
    # The layers and profiles this layout inherits: a board's state may come from a profile,
    # and every check below (RF rules, firmware, topology, groups) must see what Ansible will.
    layered = layers.resolve(layout_dir, config)
    for name, spec in sorted(boards.items()):
        spec = spec or {}
        canon = canonical(reg, name)
        if canon is None:
            problems.append(f"{name}: not in the device registry")
            continue
        entry = dict(reg["devices"][canon] or {})
        pin = (entry.get("match") or {}).get("host")
        # Where the board is plugged in now, from the plug watcher's index (location.py); the
        # layout's host, then the registry's, are only hints for a board that is not seen.
        reach_now = entry.get("reach") or {}
        hint = spec.get("host") or (reach_now.get("usb") or {}).get("host") \
            or (reach_now.get("swd") or {}).get("host")
        host, how = location.locate(entry, hint, index=seen_index)
        if not host:
            problems.append(f"{name}: not seen on any bench host, and no host declared")
            continue
        # Not seen by a running plug watcher: unplugged. Skipped by plan/apply, not a failure of
        # the whole layout. With no fresh index at all, presence is unknown: assume present.
        # A simulated board is a process on this machine, not a USB device: the plug watcher
        # never sees it, so its index does not decide whether it is there. Nor does it see a
        # chip behind a debug probe: the flasher's read of the chip's unique id decides.
        # Nor can it see a board the registry gives no identity of its own (one behind a USB-UART
        # bridge, a device reached over the network): not seeing it says nothing, so it is
        # assumed present, with a warning, and the first read of the board decides.
        nameless = not location.identity(entry) and entry.get("kind") not in ("sim", "swd")
        present = how == "seen" or not seen_index or entry.get("kind") in ("sim", "swd") or nameless
        # A chip behind a debug probe is there when its probe is: the probe is a USB device the
        # watcher knows, so with a fresh index, the probe unseen means the chip is skipped, not
        # a failure of the whole layout when the module asks devreg for the probe.
        probe = ((entry.get("reach") or {}).get("swd") or {}).get("probe") if entry.get("kind") == "swd" else None
        probe_absent = False
        if probe and seen_index:
            probe_entry = (reg["devices"].get(canonical(reg, probe) or "") or {})
            pm = probe_entry.get("match") or {}
            if location.identity(probe_entry):
                probe_seen = location.locate(probe_entry, None, index=seen_index)[1] == "seen"
            elif pm.get("usb_vid") and pm.get("usb_pid"):
                # a probe the registry knows by model alone (a clone with no usable serial)
                probe_seen = f"{pm['usb_vid']}:{pm['usb_pid']}".lower() in location.seen_models(config.get("location_dir"))
            else:
                probe_seen = True   # nothing the watcher could see it by: assumed, as before
            if not probe_seen:
                present, probe_absent = False, True
        if nameless and seen_index:
            factory = location.factory_serial(entry)
            warnings.append(
                f"{name}: its USB bridge carries the factory serial {factory}, which the plug "
                f"watcher cannot tell from another of its model: give the bridge a serial of "
                f"its own (`just cp210x-serial <host>`, core/host/bin/cp210x_serial.py). Assumed present; its first read decides"
                if factory else
                f"{name}: the registry gives it no identity the plug watcher could see it by "
                f"(a chip id or a MAC): assumed present; its first read decides")
        if probe_absent:
            warnings.append(f"{name}: its probe {probe} is not plugged in (the plug watcher does not "
                            f"see it): skipped")
        elif not present:
            warnings.append(f"{name}: not plugged in (the plug watcher does not see it): skipped")
        if how == "seen" and hint and hint != host:
            warnings.append(f"{name}: seen on {host}, though declared on {hint} (moved?)")
        if how == "seen":
            # The USB route goes where the board is, not where the registry last put it.
            reach = dict(entry.get("reach") or {})
            if "usb" in reach:
                reach["usb"] = {**(reach["usb"] or {}), "host": host}
                entry["reach"] = reach
        if pin and pin != host:
            problems.append(f"{name}: declared on {host} but the registry pins it to {pin}")
            continue
        try:
            conn = host_connection(reg, host)
        except LayoutError as e:
            problems.append(str(e))
            continue
        bench_hosts[host] = conn
        hv, _ = layers.effective(layout_dir, config, name, resolved=layered)
        all_hv[name] = hv
        fw_kind = entry.get("firmware") or "unknown"
        try:
            chosen = routes.select(entry, hv.get("routes"), default_host=host)
        except routes.RouteError as e:
            problems.append(f"{name}: {e}")
            continue
        out_boards[name] = {
            "connection": conn,
            # no_console: nothing a serial broker could own (a chip behind a debug probe, a
            # device whose shell is ssh)
            "groups": groups_for(hv, fw_kind) + [f"host_{host}", f"kind_{entry.get('kind') or 'unknown'}"]
            + ([] if present else ["absent"])
            + ([] if via_of(chosen) == "console" else ["no_console"]),
            "vars": {
                "bench_board": name,
                "bench_devreg": canon,
                "bench_access_host": host,
                "bench_present": present,
                "bench_fw_kind": fw_kind,
                # What the registry says it is (serial, swd, sim, ...); a simulated board's
                # factory image, hardware id and entropy seed, for the sim bench on its host.
                "bench_kind": entry.get("kind"),
                **({"bench_sim_exe": entry.get("sim_exe"),
                    "bench_sim_device_id": (entry.get("match") or {}).get("sim_device_id"),
                    "bench_sim_seed": entry.get("sim_seed"),
                    # pty: the firmware makes its own and names it; stdio: the bench makes one
                    "bench_sim_console": entry.get("sim_console") or "pty"} if entry.get("kind") == "sim" else {}),
                "bench_board_tag": (entry.get("policy") or {}).get("board_tag"),
                # The unit's USB serial (a chip id, a MAC): what a flasher that finds the chip
                # by it is handed (picotool's --ser).
                "bench_usb_serial": (entry.get("match") or {}).get("usb_serial"),
                # The chip's MAC as its ROM loader reads it: what an ESP32 writer checks in its
                # own session before a byte is written (policy.chip_mac, else a usb_serial
                # that is the MAC). A bridge with no serial (a CH340) names no chip otherwise.
                "bench_chip_mac": chip_mac(entry),
                "bench_may_transmit": (entry.get("policy") or {}).get("may_transmit", True),
                "bench_compatible": (entry.get("policy") or {}).get("compatible"),
                # One route per verb (shell, flash, config, diag), chosen here so it is
                # visible in `just show` before anything runs. See module_utils/routes.py.
                "bench_routes": chosen,
                # How a command reaches the device: its console (through its serial broker),
                # ssh, or None. The plays that run commands ask this, not what kind of
                # device it is.
                "bench_via": via_of(chosen),
            },
        }
    images = {}
    for name, b in out_boards.items():
        fw_problems, image = check_firmware(name, all_hv[name], catalog, b["vars"]["bench_board_tag"])
        problems += fw_problems
        # the catalog's own entry (a family's record may hold only what it judges by)
        declared_image = (families.firmware(all_hv[name]) or {}).get("image")
        images[name] = (catalog.get("images") or {}).get(declared_image) if image is not None \
            and declared_image else None
        # What the board's type is (catalog/boards.yml): the chip esptool is told, the PSRAM a
        # first flash checks. A type the catalog does not know is a refusal, not a guess.
        tag = b["vars"]["bench_board_tag"]
        if tag:
            kind = catalog["boards"].get(tag)
            if kind is None:
                problems.append(f"{name}: board type {tag!r} is not in the board catalog "
                                f"(catalog/boards.yml; a site adds its own in <site>/catalog/boards.yml)")
            else:
                b["vars"]["bench_mcu"] = kind.get("mcu")
                if kind.get("psram"):
                    b["vars"]["bench_psram"] = kind["psram"]
                # The type's whole record, for what a role or a plugin reads of it that core
                # does not name (where the port's modem lines rest, how a console is entered).
                b["vars"]["bench_board_type"] = dict(kind)
        fam = families.declared(all_hv[name])
        b["vars"]["bench_family"] = fam
        # What this family is, from its row in the families table (plugin_utils/families/): the role that brings it to its declared state,
        # the config vocabulary that picks its modules, how its config is written, and which
        # field names its image. Roles and playbooks gate on these, never on a family's name.
        # A family may narrow the row for one board (one that takes no configuration).
        plugin = families.plugin(fam)
        row = plugin.board_row(all_hv[name], families.of(fam))
        if row.get("console") is None and "no_console" not in b["groups"]:
            # A family this tool has no console for: a broker on its port would hold it for
            # nothing (and against whatever does drive the device).
            b["groups"].append("no_console")
        # The collection whose board role holds this family's task files (none for a family
        # that needs no plugin).
        if families.owner(fam):
            b["vars"]["bench_plugin"] = families.owner(fam)
        # How it is brought to its declared image: None for this tool's own flashers (the
        # `board` role), else the word naming its plugin's update step.
        b["vars"]["bench_update"] = row["update"]
        b["vars"]["bench_vocabulary"] = row["vocabulary"]
        b["vars"]["bench_config_via"] = row["config_via"]
        b["vars"]["bench_identity_key"] = row["identity_key"]
        # The secrets the board holds (the family's, narrowed for this board), each with the
        # task files that read and write it and whether the site escrows it: a layout says
        # `bench_secret_policy: {<kind>: {escrow: false}}` for one it leaves alone.
        policy = all_hv[name].get("bench_secret_policy") or {}
        kinds = []
        for s in plugin.board_secrets(all_hv[name]):
            kinds.append({**s, "tasks": "_".join(x for x in (s["kind"], s.get("via")) if x),
                          "escrow": s["origin"] == "device"
                          and (policy.get(s["kind"]) or {}).get("escrow", True) is not False})
        b["vars"]["bench_secret_kinds"] = kinds
        # how the identity key is escrowed, for a family's own tasks (None: it is not)
        b["vars"]["bench_key_escrow"] = next(
            (s.get("via") for s in kinds if s["kind"] == "identity" and s["escrow"]), None)
        # How the device is found on the bench: the family's indicator, or the one a layout
        # names for this board (a buzzer wired to it). bench_led is the family's own word.
        b["vars"]["bench_locate"] = all_hv[name].get("bench_locate") or row["locate"]
        b["vars"]["bench_led"] = row["locate"]
        if plugin.role_tasks:
            b["vars"]["bench_family_tasks"] = plugin.role_tasks
        b["vars"]["bench_diag_defaults"] = [dict(c) for c in plugin.diag_commands]
        # A family with no console that runs steps its own way (step_tasks: its USB port is its
        # API, not a shell) is not reached through a broker on that port: its board takes
        # scenario steps through the family's task file. (A family with no console row but a
        # shell, a Zephyr sample, keeps its console route.)
        if plugin.step_tasks and row.get("console") is None:
            if b["vars"]["bench_via"] == "console":
                b["vars"]["bench_via"] = None
            if b["vars"]["bench_via"] is None:
                b["vars"]["bench_step_tasks"] = plugin.step_tasks
        # What the role must know about this board that its vars do not say: the family's
        # facts, each also as bench_<key> so a task reads one name.
        facts = plugin.board_facts(all_hv[name], b["groups"])
        b["vars"]["bench_family_facts"] = facts
        for k, v in facts.items():
            b["vars"][f"bench_{k}"] = v
        # Where each firmware family's source lives on this machine (catalog `sources:`).
        b["vars"]["bench_sources"] = catalog["sources"]
        # The ranges of a chip with no layout of its own, for the write rule its flasher asks
        # (docs/ARCHITECTURE.md#partitions-and-the-write-rule): the image's, else its family's.
        declared_ranges = (image or {}).get("regions") or list(plugin.flash_regions or ())
        if declared_ranges:
            ranges, why = regions.declared(declared_ranges)
            problems += [f"{name}: flash regions: {p}" for p in why]
            b["vars"]["bench_flash_regions"] = ranges
        # How this board's image is written, from the catalog (host_vars may override).
        if image and image.get("flasher"):
            b["vars"]["bench_flasher"] = image["flasher"]
            if image["flasher"] not in families.flashers():
                problems.append(f"{name}: its image is written by flasher {image['flasher']!r}, which "
                                "neither core nor a loaded plugin brings (a plugin declares its "
                                "flashers in FLASHERS, docs/PLUGINS.md)")
        # A chip reached only through a debug probe is flashed through it: by its image's
        # flasher when that one writes over a probe (a plugin's, which says so in
        # FLASHER_BEARERS), else by core's swd flasher. An image whose flasher writes over
        # another bearer cannot reach it that way.
        if (b["vars"]["bench_routes"].get("flash") or {}).get("bearer") == "swd":
            own = image.get("flasher") if image else None
            flasher, why = probe_flasher(own, families.flasher_bearers(own) if own else None)
            if flasher:
                b["vars"]["bench_flasher"] = flasher
            if why:
                problems.append(f"{name}: {why}")
        # A flash route that carries the image itself (a plugin's ROUTE_DELIVERIES): the
        # family's update step hands the write to it, when the family can take one.
        delivery = families.route_delivery((b["vars"]["bench_routes"].get("flash") or {}).get("bearer"))
        if delivery:
            takes = [f for f in delivery["formats"] if f in plugin.takes_delivery]
            if takes:
                b["vars"]["bench_delivery"] = delivery
            else:
                problems.append(
                    f"{name}: its flash route is {delivery['bearer']}, which delivers an image "
                    f"itself ({', '.join(delivery['formats'])}); family {fam!r} cannot take a "
                    "delivered image (its Family.takes_delivery, docs/PLUGINS.md)")
        # What can be done to a device flashed this way beside writing its image (its
        # config regions and its whole chip backed up, restored, erased): flash/common.py.
        b["vars"]["bench_can"] = families.flasher_can(
            all_hv[name].get("bench_flasher") or b["vars"].get("bench_flasher"))
        if (entry.get("kind") or "") == "sim":
            # a simulated board's chip is its flash file, whatever flashes it (flash/simchip.py)
            b["vars"]["bench_can"] = sorted(set(b["vars"]["bench_can"]) | {"chip"})
        # The region level is the family's to have: regions its image's flash map names, or
        # ranges declared for the chip with a config role (docs/ARCHITECTURE.md#chip-and-region-flows).
        # A family
        # with neither (stock firmware, a bare image) has no region level, whatever flasher.
        flasher = all_hv[name].get("bench_flasher") or b["vars"].get("bench_flasher")
        only = plugin.region_flashers
        declared_config = regions.config_ranges(b["vars"].get("bench_flash_regions"))
        if "regions" in b["vars"]["bench_can"] and (
                not (plugin.config_regions or declared_config) or (only and flasher not in only)):
            b["vars"]["bench_can"] = [c for c in b["vars"]["bench_can"] if c != "regions"]
        # The plugin's bootloader step for a board flashed this way, where it has one.
        # The family's own chip task files for this flasher (Family.chip_tasks), only when it
        # names some: the plays read `bench_chip_tasks | default({})`.
        chip_tasks = plugin.chip_tasks.get(all_hv[name].get("bench_flasher") or b["vars"].get("bench_flasher"))
        if chip_tasks:
            b["vars"]["bench_chip_tasks"] = dict(chip_tasks)
        step = plugin.bootloader_tasks.get(all_hv[name].get("bench_flasher") or b["vars"].get("bench_flasher"))
        if step:
            b["vars"]["bench_bootloader_tasks"] = step
    # The RF ceiling is the site's (or the file the layout names); the layout's own rf_rules may
    # only tighten it, and every board carries the rules it was checked against.
    try:
        ceil = rflayout.ceiling(local(config.get("rf_ceiling")) or site.rf_rules_path())
    except ValueError as e:
        raise LayoutError(f"layout refused:\n  {e}") from e
    rules, rule_problems = rflayout.tighten(ceil, config.get("rf_rules"))
    problems += rule_problems
    for b in out_boards.values():
        b["vars"]["bench_rf_rules"] = rules
    problems += rflayout.check_layout(all_hv, rules)
    # What an image transmits before its config is written (a first flash, a switch): not a
    # refusal of the layout (the board may run it already), but of a fresh write of it.
    for name, b in out_boards.items():
        boot = rflayout.boot_radio(name, images.get(name), rules)
        b["vars"]["bench_boot_radio_problems"] = boot
        if boot and not all_hv[name].get("bench_boot_radio_ok"):
            warnings.append(f"{name}: its image boots on its own radio ({'; '.join(boot)}): a "
                            "first flash or a family switch to it is refused unless "
                            "bench_boot_radio_ok")
    # How much the checks read (docs/ARCHITECTURE.md#checking-depth): full or spot, nothing else.
    for name in sorted(out_boards):
        asked = all_hv[name].get("bench_check_depth", "full")
        if asked not in ("full", "spot"):
            problems.append(f"{name}: bench_check_depth is {asked!r}: full (the default) or spot")
    # What a plan does with what the survey found on a chip the layout does not own.
    for name in sorted(out_boards):
        foreign = all_hv[name].get("bench_foreign", "info")
        if foreign not in ("info", "warn", "refuse"):
            problems.append(f"{name}: bench_foreign is {foreign!r}: info (the default), warn or refuse")
    # The regions a write may take (docs/ARCHITECTURE.md#partitions-and-the-write-rule): partition labels, or all.
    for name in sorted(out_boards):
        over = all_hv[name].get("bench_overwrite", [])
        if not isinstance(over, list) or not all(isinstance(x, str) and x for x in over):
            problems.append(f"{name}: bench_overwrite is {over!r}: a list of partition labels, "
                            "or [all]")
    # Which family's way of asking for UF2 mode an app takes, when no read can say
    # (docs/ARCHITECTURE.md#entering-uf2-mode): a loaded family that has one.
    def asks_uf2(fam):
        ways = families.plugin(fam).spec(families.of(fam)).get("uf2_enter") or {}
        return bool(ways.get("shell_command") or ways.get("app_tools"))
    for name in sorted(out_boards):
        enter_as = all_hv[name].get("bench_uf2_enter_as")
        if enter_as is None:
            continue
        if not (families.known(enter_as) and asks_uf2(enter_as)):
            have = sorted(f for f in families.FAMILIES if asks_uf2(f))
            problems.append(f"{name}: bench_uf2_enter_as is {enter_as!r}, which is "
                            + ("no loaded family" if not families.known(enter_as) else
                               "a family that says nothing of how to ask for UF2 mode")
                            + f": one of {', '.join(have) or 'none loaded'}")
    # A family no loaded plugin brings: core compares what it knows of any board (its image, by
    # its bytes) and nothing else, so a plan of 0 must not read as "all as declared".
    for name in sorted(out_boards):
        fam = families.declared(all_hv[name])
        if fam and not families.known(fam):
            warnings.append(f"{name}: no loaded plugin brings its family {fam}: the plan compares "
                            "its image only; its config and anything else declared for it are "
                            "not compared")
    # What a family's own block makes possible across boards (boards that name each other,
    # roles that contradict): each family present checks the layout once.
    for fam in sorted({families.declared(hv) for hv in all_hv.values()}, key=str):
        problems += families.plugin(fam).check_layout(all_hv)
    for name, b in out_boards.items():
        if not b["vars"]["bench_may_transmit"] and rflayout.transmits(all_hv[name]):
            fam = families.declared(all_hv[name])
            why = (f"gives it {fam} firmware, whose radio this tool does not set: it would transmit "
                   f"at that firmware's defaults") if families.radio(fam) == "unmanaged" else "enables TX"
            problems.append(f"{name}: the registry says it must never transmit, but the layout {why}")
    if problems:
        raise LayoutError("layout refused:\n  " + "\n  ".join(problems))
    return {"bench_hosts": bench_hosts, "boards": out_boards, "families": families.specs(),
            "warnings": warnings + rflayout.warnings(all_hv)}
