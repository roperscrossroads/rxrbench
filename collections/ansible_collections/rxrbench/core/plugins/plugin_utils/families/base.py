"""What a family can answer, with the generic answer for each question.

A family's plugin subclasses Family and overrides what it knows. Generic code (the inventory,
the layout checks, the RF rules, the flashers, the role, the tools) calls these and never
names a family. A plugin imports only leaf modules (recipe, intents, attach, artifact, enums):
layout, rfrules and flash import this package, so the other direction would be a cycle.

The hooks are added one concern at a time; a hook nobody calls yet is listed here so the
shape is visible, with the default that today's generic code behaves as.
"""


#: What a kind of console gives every family that has it: how a board is rebooted through it
#: and its uptime read, and how it is asked which Bluetooth bonds it holds.
CONSOLES = {
    "zephyr-shell": {"reboot": {"reboot": "kernel reboot cold",
                                "uptime": {"command": "kernel uptime",
                                           "pattern": r"Uptime:\s*(\d+)\s*ms", "scale": 0.001}},
                     # Zephyr's Bluetooth stack keeps a bond under bt/keys/<address>
                     "bonds_command": "settings list bt/keys", "bonds_prefix": "bt/keys/"},
}


class Family:
    #: How the family's state line is read into an identity (familyspec.py `identity`, less
    #: the command, which is the row's `state_command`), or None when it prints none.
    state = None

    #: The manifest fields an image of this family is told apart by: each must equal the
    #: board's declaration before the image is written.
    image_match = ()

    #: A field of the manifest that is also read from the image's own bytes ({field, args}).
    image_bytes_check = None

    #: How a board is rebooted through its console, when the console's kind does not say.
    reboot = None

    #: The task file, in the plugin's `board` role, that brings a board's bootloader to the
    #: declared one, by the board's flasher ({"uf2": "bootloader_uf2"}): `just boot` includes
    #: it for a bootloader the tool has no flasher of its own for.
    bootloader_tasks = {}

    #: The task files in the plugin's board role that back up, write back and erase a chip its
    #: own flasher writes ({"<flasher>": {"backup": ..., "restore": ..., "erase": ...}}, any of
    #: the three): `just backup`, `just restore-chip` and `just erase` include them for a board
    #: flashed that way, where core has no module that knows the flasher. Each registers what
    #: core's module would (`chip_backup`: {path, chip, files}; `restore`: {path, changed, msg};
    #: `erased`: {msg}), guards the device itself (`devreg guard`, as a flasher does) and is
    #: entered only past core's go-ahead.
    chip_tasks = {}

    #: The secrets a device of this family holds: [{kind, origin, via?}]. `origin` is `device`
    #: (the device made it; the site escrows a copy and can give it back) or `declared` (the
    #: site is the authority and the device must match). The plugin's `board` role has
    #: `secret_<kind>[_<via>]_read.yml` for each, which sets `board_secret`, and for a `device`
    #: secret `..._write.yml`, which gives the escrowed one (`board_escrowed`) back.
    secrets = ()

    #: What asks a running app for its bootloader's UF2 mode (familyspec.py `uf2_enter`).
    uf2_enter = None

    #: The ranges of a chip with no layout of its own (no partition table, no flash map in its
    #: images): [{label, kind: boot|app|data, start, size, role?: config}], addresses
    #: absolute. A catalog image's own `regions` win (docs/ARCHITECTURE.md#partitions-and-the-write-rule).
    flash_regions = ()

    #: For firmware with no state line: how what a board runs is read by a probe this plugin
    #: deploys (familyspec.py `identity`, the `tool` form): {tool, fields, build_from, timeout}.
    identity_probe = None

    #: How the survey recognises this family's firmware on a USB device (familyspec.py
    #: `survey`): the kinds of device asked for its state line and what is kept of it, or the
    #: probe tool the plugin deploys for a firmware that has none.
    survey = None

    #: Image features (`firmware.features`) this family's recipes know, in the order an image's
    #: identity lists them (identity.of_state filters a state line's features by these).
    features = ()

    #: Groups a feature puts a board in, beyond `feature_<name>`.
    feature_groups = {}

    #: The task file, in the plugin's `board` role, that runs a scenario step on a board of
    #: this family that has no console and no ssh (its own API): handed `step` and
    #: `step_name`, it registers `board_step_said` as `device_run` returns it. Published as
    #: bench_step_tasks for such a board only. None: such a board takes no steps.
    step_tasks = None

    #: The task file under roles/board/tasks/family/ run after the vocabulary's, or None.
    role_tasks = None

    #: What `just diag` asks a device of this family when its layout does not say
    #: (bench_diag_commands): [{name, cmd, timeout?}], published as bench_diag_defaults.
    diag_commands = ()

    #: The console command that says why the board last reset (shown after `just reboot`),
    #: or None.
    reset_command = None

    #: The device's error counters, for a scenario's health readings (`health: true`): {cmd,
    #: and plugins/filter/counters.py's spec: extract, path, ignore, allow, restart,
    #: optional}. Published as bench_health_counters; None: its boards have none.
    health_counters = None

    #: Config regions of an ESP32 image of this family (name, key in its flash map), or None.
    #: The map is the build's flash.json, or a merged image's own partition table (each data
    #: partition by its label in capitals: `spiffs` is `SPIFFS`).
    config_regions = None

    #: The flashers whose images name those regions, or None for any flasher with a region
    #: level: a stock firmware's merged ESP32 image carries its table, its UF2 for an nRF52 or
    #: RP2040 names none.
    region_flashers = None

    #: The image formats this family's update step takes from a route that delivers an image
    #: itself (a plugin's ROUTE_DELIVERIES): its flasher then reads the device, hands the write
    #: to the route, and reads the device again. Empty: the family is only written by the
    #: bench host, and a layout that pins such a route for it is refused.
    takes_delivery = ()

    def release_partitions(self, member, read):
        """What a release's full install writes beside its merged ESP32 image (an `esp-image`
        catalog entry whose `member` is that image), as [{partition, member, md5?}]: the
        partition by its label or subtype name, another file of the same release, and the md5
        the release gives it, when it gives one. `read(name)` is a member's bytes, or None when
        the release has no such member. Generic: nothing (an entry can name them itself, as
        `partition_images`)."""
        return []

    def catalog(self, dirs):
        """This family's own catalog, read from the catalog directories in order (later over
        earlier): what `image()` resolves a declaration against, as `catalog["families"][f]`.
        Generic: nothing (listed images are the firmware catalog's, `catalog["images"]`)."""
        return {}

    def image(self, fw, catalog):
        """(record, problems) for a board's `firmware:` declaration. record: `board_tags` (the
        board types the image is built for) and `flasher`; None when the declaration names
        nothing the catalog has, with the problem said. Generic: a listed image of the
        firmware catalog, which must be this family's and have a source."""
        from ...module_utils import artifact  # a leaf; imported here so the package stays import-light
        img = catalog["images"].get(fw.get("image"))
        if img is None:
            return None, [f"image {fw.get('image')!r} is not in the firmware catalog"]
        problems = []
        if img.get("family") != fw.get("family"):
            problems.append(f"image {fw['image']} is {img.get('family')} firmware, "
                            f"the board declares {fw.get('family')}")
        problems += [f"image {fw['image']}: {p}" for p in artifact.check_source(img.get("source"))]
        rec = {"board_tags": list(img.get("board_tags") or []), "flasher": img.get("flasher")}
        if img.get("regions"):   # declared ranges, for a chip with no table of its own
            rec["regions"] = img["regions"]
        return rec, problems

    def groups(self, hostvars):
        """Inventory groups a board belongs to from this family's own block, beyond the generic
        ones (family_<f>, <identity_key>_<v>, feature_<f>)."""
        return []

    def board_row(self, hostvars, row):
        """This board's effective row of the families table: a family may narrow it for one
        board (one that takes no configuration)."""
        return row

    def board_secrets(self, hostvars):
        """The secrets this board holds: the family's, less what this one board does not
        have (a board with no key of its own)."""
        return [dict(s) for s in self.secrets]

    def board_facts(self, hostvars, groups):
        """What the family's task files need to know about this board beyond its vars,
        published as bench_family_facts and each lifted to bench_<key>: where something is
        kept on its devices, which of its steps one board leaves to another. Generic: nothing."""
        return {}

    def check_layout(self, all_hostvars):
        """Problems across the boards of one layout that this family's block makes possible
        (topology, roles that contradict). A list of strings; empty is clean."""
        return []

    def rf_declared(self, name, hostvars, managed):
        """What the board's radio is declared to do: {"transmits": True, False, or None when
        nothing is declared; "emissions": [{key, power_dbm, freq_mhz?, bw_khz?}], which the
        ceiling is applied to; "problems": what is wrong with the declaration itself}.
        `managed`: this tool sets the family's radio, so a board that declares none is a
        problem. Generic: a board with no radio declares nothing."""
        return {"transmits": None, "emissions": [], "problems": []}

    def rf_check(self, name, hostvars, rules):
        """Violations by what its own block makes transmit beyond the board itself."""
        return []

    def spec(self, row):
        """This family as data, for code that runs on a bench host and cannot ask the plugin
        (familyspec.py says what each key is)."""
        console = CONSOLES.get(row["console"]) or {}
        identity = None
        if row["state_command"] and self.state is not None:
            identity = {"command": row["state_command"], "features": list(self.features),
                        **self.state}
        elif self.identity_probe:
            identity = dict(self.identity_probe)
        return {"console": row["console"], "identity_key": row["identity_key"], "identity": identity,
                "image": {"match": list(self.image_match), "bytes_check": self.image_bytes_check},
                "regions": [list(r) for r in self.config_regions] if self.config_regions else None,
                "reboot": self.reboot or console.get("reboot"),
                "reset_command": self.reset_command,
                "bonds_command": console.get("bonds_command"),
                "bonds_prefix": console.get("bonds_prefix"), "uf2_enter": self.uf2_enter,
                "survey": self.survey}

    def enroll_hostvars(self, name, fw, facts):
        """A new board's host_vars when `just enroll --layout` adds it with this firmware
        (`fw`, its `firmware:` block): the declaration, and what else a first apply needs."""
        return {"firmware": fw}

    def survey_tags(self, probe, boards=None):
        """Capability tags for a surveyed board from this family's probe of it (the survey
        keys probes by family), with the board types at hand for an MCU lookup."""
        return set()

    def enroll_note(self, probe):
        """What a surveyed board's probe adds to its registry entry when it is enrolled:
        {"model": a hardware model name, "delivered": what it arrived running, "alias": a
        name it already goes by, "identity": what it said of itself, kept as the entry's
        `identity`}, each optional."""
        return {}

    def chip_regions(self, hostvars, partitions):
        """What this board's declaration owns on its chip beyond the firmware the chip boots,
        from the survey's record of it (`partitions`: an ESP32's `manager`, `rows`, `boot` and
        `inventory`): {"owned": [region labels], "describe": one line about the chip, or ""}.
        A family that installs an app manager owns the manager's own slot, the apps its
        declaration names and, when every image on the chip is one of those, the data they
        share (docs/ARCHITECTURE.md#partitions-and-the-write-rule). It narrows what
        the plan reports only; the write rule asks the chip and stands without it. Generic:
        nothing beyond the boot app, which core already counts as the layout's."""
        return {"owned": [], "describe": ""}

    def survey_describe(self, probe):
        """One line about what a surveyed board of this family runs, or ''. Asked only of a board
        this family's probe found (its record may be empty: found, nothing more known)."""
        return ""
