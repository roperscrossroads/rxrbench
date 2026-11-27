"""The zephyrtastic family: what it knows beyond its row (base.py lists the questions)."""
import os

from ansible_collections.rxrbench.core.plugins.module_utils import rfrules, site
from ansible_collections.rxrbench.core.plugins.plugin_utils.families import base
from ansible_collections.rxrbench.mesh.plugins.module_utils import config, hub, intents, meshrf, recipe

#: This collection's own catalog (catalog/zephyrtastic.yml).
CATALOG = os.path.abspath(os.path.join(os.path.dirname(__file__), *[".."] * 3, "catalog"))

#: The bench LED's colour by group, first match wins; a layout's `board_led_colors` replaces the
#: list (a site adds its clusters' colours there), a board's `zephyrtastic.led.color` beats both.
LED_COLORS = [{"group": "hubs", "color": "magenta"}, {"group": "workers", "color": "cyan"},
              {"group": "listeners", "color": "yellow"}, {"group": "boards", "color": "red"}]


#: (key of the survey's probe, the role it means): a state line's `hub`/`worker` objects, and
#: `brain`/`head` from firmware before the rename, are the same roles.
SURVEY_ROLES = (("hub", "hub"), ("brain", "hub"), ("worker", "worker"), ("head", "worker"),
                ("scanner", "listener"))


def _untemplated(sec):
    """A security block with the admin keys a template still stands for left out of the format
    check (they come from the site's secrets at run time); their count and presence stay."""
    if not isinstance(sec, dict):
        return sec
    keys = sec.get("admin_key")
    if isinstance(keys, str) and "{{" in keys:      # the whole list is the site's to give
        return {**sec, "admin_key": ["hex:" + "00" * 32]}
    if not isinstance(keys, (list, tuple)):
        return sec
    if any(isinstance(k, str) and "{{" in k for k in keys):
        return {**sec, "admin_key": ["hex:" + f"{i:02x}" * 32 for i in range(len(keys))]}
    return sec



def resolve_class(value, classes):
    """The class number a layout means by `value`: a number (or its string), or a class's
    bundle name (the catalog's `name`). None when it names no class of the catalog."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value in classes else None
    v = str(value).strip()
    if v.isdigit():
        return int(v) if int(v) in classes else None
    for cid, cls in classes.items():
        if cls.get("name") == v:
            return int(cid)
    return None

class Family(base.Family):
    #: a UF2 board's bootloader is this plugin's to update (roles/board/tasks/bootloader_uf2.yml)
    bootloader_tasks = {"uf2": "bootloader_uf2"}
    #: an image with the hub feature puts its board among the hubs
    feature_groups = {"hub": "hubs"}
    #: a feature's former name (brain, now hub) is the same feature
    feature_aliases = dict(recipe.FEATURE_ALIASES)
    #: roles/board/tasks/family/zephyrtastic.yml: the LED, intents, a hub's workers, a listener's presets
    role_tasks = "zephyrtastic"

    reset_command = "meshtastic resets"
    #: its identity key, read and given back through the phone API tunnel; the channel keys
    #: and the admin keys its layout declares
    secrets = ({"kind": "identity", "origin": "device", "via": "tunnel"},
               {"kind": "channel_keys", "origin": "declared"},
               {"kind": "admin_keys", "origin": "declared"})
    #: `meshtastic state`; firmware from before it still says build, board, image and class
    #: in `meshtastic version`
    state = {"version": 1, "strip_build": True,
             "fields": {"target": "board", "version": "image", "build": "build", "class": "class",
                        "features": "feat"},
             "fallback": {"command": "meshtastic version", "require": "build:",
                          "fields": {"build": r"^build:\s+(\S+)", "target": r"^board:\s+(\S+)",
                                     "version": r"^image:\s+(\d+\.\d+\.\d+\+\d+) ",
                                     "class": r"^class:\s+(\d+)"},
                          "optional": ["version"], "ints": ["class"]}}
    #: its shell asks the bootloader for UF2 mode; a board still on stock Meshtastic is asked
    #: by the stock CLI, where a bench host has it
    uf2_enter = {"shell_command": "meshtastic dfu", "shell_usb": ["2fe3"], "bootloader": "OTAFIX",
                 "app_tools": ["~/.venv-mesh/bin/meshtastic --port {port} --enter-dfu",
                               "~/.local/bin/meshtastic --port {port} --enter-dfu"]}
    #: its state line is asked of a Zephyr console on any of these kinds of USB device
    survey = {"kinds": ["zephyr-cdc", "esp32-usb-jtag", "esp32-usb-cdc"],
              "keep": {"node_id": "id", "board": "board", "class": "class", "build": "build",
                       "image": "image", "features": "feat",
                       # the front end detected at boot (firmware with lora.fem in its state line)
                       "fem": "lora.fem"},
              # a hub, a worker (before the rename: brain, head), a listener
              "flags": {"hub": "hub", "worker": "worker", "brain": "brain", "head": "head",
                        "scanner": "scan"}}
    #: an image goes on a board only as the class, with the features, the layout declares;
    #: the class is also in the image's signed TLV
    image_match = ("class", "features")
    image_bytes_check = {"field": "class", "args": ["--class"]}
    #: the recipe features an image's identity lists (a hub is told from a plain member by them)
    features = tuple(recipe.FEATURES)
    #: an ESP32 image's config regions: Zephyr's settings store (NVS) and the bulk store
    config_regions = (("storage", "STORAGE"), ("bulk", "MT_BULK"))
    #: its communication counters, the `health` object of `meshtastic state` (firmware 0.4.55
    #: on; older firmware has none, and is not judged), sorted by the firmware into operations,
    #: retries, warnings and errors: a scenario that asks is judged on the errors and keeps the
    #: change in the rest (core's module_utils/readings.py)
    health_counters = {"cmd": "meshtastic state", "extract": r"~S(\{.*\})\*[0-9a-f]{4}",
                       "path": "health", "restart": "up", "optional": True,
                       "judge": "err", "record": ["ops", "retry", "warn"]}
    #: what `just diag` asks a board of this family (its shell), unless the layout says
    diag_commands = ({"name": "version", "cmd": "meshtastic version"},
                     {"name": "state", "cmd": "meshtastic state"},
                     {"name": "status", "cmd": "meshtastic status"},
                     {"name": "lora", "cmd": "meshtastic lora"},
                     {"name": "resets", "cmd": "meshtastic resets"})

    def survey_tags(self, probe, boards=None):
        """What the firmware's state line says of a board: its MCU (from the Zephyr board name,
        through the board types), class, board, role."""
        t = {"fw:zephyrtastic", "radio:lora"}
        board = probe.get("board") or ""
        for kind in (boards or {}).values():
            if any(board.startswith(z) for z in kind.get("zephyr_boards") or ()):
                if kind.get("mcu"):
                    t.add(f"mcu:{kind['mcu']}")
                break
        if probe.get("class") is not None:
            t.add(f"class:{probe['class']}")
        if board:
            t.add(f"board:{board}")
        for key, role in SURVEY_ROLES:
            if key in probe:
                t.add(f"role:{role}")
        return t

    def survey_describe(self, probe):
        if not probe.get("node_id"):
            return ""
        roles = sorted({role for key, role in SURVEY_ROLES if probe.get(key)})
        return (f"zephyrtastic class {probe.get('class')} on {probe.get('board')}, build "
                f"{probe.get('build')}, node {probe.get('node_id')}"
                + (f", {'/'.join(roles)}" if roles else ""))

    def enroll_hostvars(self, name, fw, facts):
        """A new node: its class, an owner named after it, the firmware's default preset in the
        site's region at the most the site allows, the default primary channel."""
        short = name if len(name) <= 4 else name[0] + name[-3:]
        try:
            rules = rfrules.ceiling(site.rf_rules_path())
        except ValueError:
            rules = {}
        region = next((n for n, code in meshrf.REGION_CODES.items() if code == rules.get("region")), None)
        lora = {**({"region": region} if region else {}), "modem_preset": "LONG_FAST",
                **({"tx_power": rules["max_tx_power"]} if rules.get("max_tx_power") else {}),
                "tx_enabled": True}
        return {"firmware": fw,
                "meshtastic": {"owner": name, "owner_short": short,
                               "config": {"lora": lora,
                                          "device": {"role": "CLIENT", "rebroadcast_mode": "NONE"}},
                               "channels": [{"index": 0, "name": "", "role": "PRIMARY",
                                             "psk": "default"}]}}

    def groups(self, hostvars):
        """From the board's `zephyrtastic:` block: a stock board, a worker, a listener, a
        cluster member."""
        zep = hostvars.get("zephyrtastic") or {}
        g = []
        if zep.get("stock"):
            g.append("stock")
        if hub.is_worker(zep):
            g.append("workers")
        if zep.get("listener"):
            g.append("listeners")
        if zep.get("cluster"):
            g.append(f"cluster_{zep['cluster']}")
        return g

    def board_row(self, hostvars, row):
        """A keyless worker has no phone API: it takes no config and escrows no key."""
        if hub.is_worker(hostvars.get("zephyrtastic")):
            return {**row, "config_via": None}
        return row

    def board_secrets(self, hostvars):
        """A keyless worker holds none: it has no identity key and takes no channels."""
        if hub.is_worker(hostvars.get("zephyrtastic")):
            return []
        return [dict(s) for s in self.secrets]

    def board_facts(self, hostvars, groups):
        facts = self._board_facts(hostvars, groups)
        # A layout may name its class by number or by bundle name; what the tasks and the
        # site's templates use (the store key class-N, the version probe) is the number.
        fw = hostvars.get("firmware") or {}
        if fw.get("family") == "zephyrtastic" and fw.get("class") is not None:
            classes = self.catalog([CATALOG])["classes"]
            cid = resolve_class(fw.get("class"), classes)
            facts["firmware_class"] = cid
            facts["firmware_bundle"] = (classes.get(cid) or {}).get("name") if cid is not None else None
            # The features in their current names (the store key), and in the names an image
            # staged before a rename carries (family_zephyrtastic.yml's bench_image falls back
            # to that spelling when no image of the new one is staged).
            old = {new: o for o, new in self.feature_aliases.items()}
            facts["firmware_features"] = recipe.canon(sorted(fw.get("features") or []))
            facts["firmware_features_old"] = sorted(old.get(f, f) for f in facts["firmware_features"])
        return facts

    def _board_facts(self, hostvars, groups):
        """A worker's preset is its hub's to set (the hub re-sends it), so the radio step
        leaves it alone; a listener sweeps presets, so where its radio is now is not where it
        is stored to be. `hub_workers`: the workers a hub declares ({board: preset}), in
        either spelling of the block, or None."""
        zep = hostvars.get("zephyrtastic") or {}
        led = zep.get("led") or {}
        colors = hostvars.get("board_led_colors") or LED_COLORS
        return {"radio_skip": ["modem_preset"] if hub.is_worker(zep) else [],
                "hub_workers": hub.declared_workers(zep),
                "radio_check_live": not zep.get("listener"),
                "led_mode": led.get("mode") or hostvars.get("board_led_mode") or "in-use",
                "led_color": led.get("color")
                or next((m["color"] for m in colors if m["group"] in groups), None)}

    def rf_declared(self, name, hostvars, managed):
        return meshrf.meshtastic_declared(name, hostvars, managed)

    def rf_check(self, name, hostvars, rules):
        """The preset a board transmits on, and the presets a hub sets on its workers (it
        re-sends them, and a worker's own radio step leaves its preset alone): each must be one
        the firmware has, and fit the band."""
        zep = hostvars.get("zephyrtastic") or {}
        out = meshrf.meshtastic_rf_check(name, hostvars, rules, own=not hub.is_worker(zep))
        for worker, preset in sorted((hub.declared_workers(zep) or {}).items()):
            out += meshrf.preset_problems(name, preset, rules, f"the preset it sets on worker {worker}")
        return out

    def check_layout(self, all_hostvars):
        """Cross-board consistency: every worker a hub names exists and names that hub back,
        a board with workers has the hub feature, a board that declares `intents: none`
        declares no role beside it, and a board's security config can be written and do not
        lock every admin out (a managed node needs an admin key)."""
        out = []
        for name, hv in sorted(all_hostvars.items()):
            zep = hv.get("zephyrtastic") or {}
            sec = ((hv.get("meshtastic") or {}).get("config") or {}).get("security")
            if sec is not None:
                if hub.is_worker(zep):
                    out.append(f"{name}: a keyless worker has no phone API and takes no "
                               "meshtastic.config.security")
                else:
                    try:
                        config.check_security(_untemplated(sec))
                    except config.ConfigError as e:
                        out.append(f"{name}: {e}")
            for old, new in (("brain", "hub"), ("head", "worker")):
                if old in zep and new in zep:
                    out.append(f"{name}: declares both zephyrtastic.{new} and its old name "
                               f"zephyrtastic.{old}; keep {new}")
            workers = hub.declared_workers(zep) or {}
            feats = recipe.canon((hv.get("firmware") or {}).get("features"))
            if workers and "hub" not in feats:
                out.append(f"{name}: declares workers but its firmware lacks the 'hub' feature")
            for w in workers:
                wz = (all_hostvars.get(w) or {}).get("zephyrtastic") or {}
                if w not in all_hostvars:
                    out.append(f"{name}: worker {w} is not a board in this layout")
                elif hub.declared_hub(wz) != name:
                    out.append(f"{name}: worker {w} does not declare hub {name}")
            h = hub.declared_hub(zep)
            if h is not None:
                hz = (all_hostvars.get(h) or {}).get("zephyrtastic") or {}
                if h not in all_hostvars:
                    out.append(f"{name}: its hub {h} is not a board in this layout")
                elif name not in (hub.declared_workers(hz) or {}):
                    out.append(f"{name}: hub {h} does not list it as a worker")
            roles = intents.conflicts(zep)
            if roles:
                out.append(f"{name}: declares intents: none and a role ({', '.join(roles)}): "
                           "a plain node remembers no role; drop one of the two")
        return out

    def catalog(self, dirs):
        """The fleet classes: `classes:` of zephyrtastic.yml in each catalog directory, later
        over earlier, keyed by class number."""
        # Imported here, not at the top: every flash module loads this plugin on the bench
        # host, and one of them (smp_flash) runs in a venv that has no PyYAML. The catalog is
        # only read on the controller.
        import yaml  # pylint: disable=import-outside-toplevel
        classes = {}
        # The tool's catalog directory holds no family's recipes: this plugin's stands there.
        tool = site.CATALOG
        for d in (CATALOG if os.path.abspath(d) == tool else d for d in dirs):
            p = os.path.join(d, "zephyrtastic.yml")
            if os.path.exists(p):
                with open(p) as fh:
                    classes.update((yaml.safe_load(fh) or {}).get("classes") or {})
        return {"classes": classes}

    def image(self, fw, catalog):
        """A class of the catalog, with the board's features applied to its recipe."""
        classes = catalog["families"]["zephyrtastic"]["classes"]
        cid = resolve_class(fw.get("class"), classes)
        cls = classes.get(cid) if cid is not None else None
        if cls is None:
            return None, [f"class {fw.get('class')!r} is not in the catalog (a number or a "
                          f"bundle name: {', '.join(sorted(str(c.get('name')) for c in classes.values()))})"]
        problems = []
        try:
            recipe.overlays_for(cls, fw.get("features"))
        except recipe.RecipeError as e:
            problems.append(str(e))
        return {"board_tags": [cls.get("board_tag")], "flasher": cls.get("flasher")}, problems
