# Plugins: adding device families

Core knows two device families (`zephyr_mcumgr`, `bare`). Every other family comes from a
**plugin**: an Ansible collection a site names. Five are bundled: `rxrbench.mesh`,
`rxrbench.linux`, `rxrbench.zephyr`, `rxrbench.nuttx` and `rxrbench.launcher`. The smallest real
one is `rxrbench.nuttx`; the smallest whole one is the test fixture
`tests/fixtures/collections/ansible_collections/fixture/widgets`. How core uses what a plugin
gives it is [ARCHITECTURE.md](ARCHITECTURE.md).

A plugin can do anything a bundled plugin does through the hooks on this page, and core never
names a plugin, family or board type in code ([ARCHITECTURE.md](ARCHITECTURE.md#core-portability)).
What a plugin needs of core goes through these hooks, not around them.

## Loading one

```yaml
# <site>/plugins.yml
plugins: [rxrbench.mesh, acme.widgets]
```

`RXRBENCH_PLUGINS` (comma-separated) overrides the file; a site without one loads what
`site.example/plugins.yml` does. A site's own collections live in
`<site>/collections/ansible_collections/<namespace>/<name>/`; `ansible.cfg` names
`./site/collections`, so a site elsewhere adds its own path to `ANSIBLE_COLLECTIONS_PATH`. A family,
`Family` object or flasher name two plugins (or a plugin and core) both bring is refused at load.

## What a plugin is

```
<namespace>/<name>/
  plugins/plugin_utils/bench.py       the exports below                                  required
  plugins/plugin_utils/families/      its Family subclasses
  plugins/modules/, module_utils/     its own modules and what they share
  roles/board/tasks/                  the task files core's plays include (Task files)
  roles/board/meta/argument_specs.yml what each of those task files reads, typed
  catalog/                            board types (boards.yml, layered over core's), image recipes
  host/bin/*.py                       tools deployed to ~/rxrbench/bin on a bench host
  host/dialects.d/*.json              console dialects for the serial broker
  host/host.yml, host/venvs/*.txt     venvs and pinned downloads it needs on a bench host
  requirements.in, requirements.txt   what it needs in the tool's venv (`just setup`)
  <name>.just                         its recipes, imported by the top-level justfile
```

`roles/board/tasks/main.yml` is a no-op: the role is a library of task files named by the
family's row.

## What a plugin exports

`plugins/plugin_utils/bench.py` exports, as module attributes (read by
`core/plugins/plugin_utils/families/__init__.py::load`):

| Export | Shape | What it is |
|---|---|---|
| `FAMILIES` | `{family: row}` | **Required.** One row per family it brings (fields below) |
| `PLUGINS` | `{name: Family()}` | The `Family` objects its rows name as `plugin`. A row with none gets the generic answers |
| `USB_KINDS` | `{"vid:pid": {kind, mcu, mode, serial_is, note}}` | The USB ids of its boards, so the survey tells a bootloader from an app. A row for an id core has replaces core's; ids read off a USB serial by a rule are marked `ids_from: usb serial` until the firmware confirms them |
| `FLASHERS` | `{"<flasher>": ("chip", "regions")}` | Flashers its images name that core does not have, with the levels a device written that way supports beside the image write (`()` for none). A layout whose image names an unknown flasher is refused |
| `FLASHER_BEARERS` | `{"<flasher>": ["swd"]}` | The bearers each of its flashers writes over. A board whose flash route is `swd` is written by its image's flasher when that one writes over `swd`, else refused |
| `RECOGNISERS` | `[{name, at, magic, u32le?}]` | What its images look like at the start of a region, so core's write rule sees an image of its kind (`module_utils/regions.py`). Says an image is there, never whose |

A row's fields (a field left out takes the "nothing" value):

| Field | What it decides |
|---|---|
| `vocabulary` | its config language: names `<vocabulary>.yml` (config) and `facts_<vocabulary>.yml` |
| `state_command` | the console command printing its [state line](#the-state-line) |
| `identity_key` | the field beside build and version that names its image (`class`, `role`) |
| `build` | `catalog` (built here from its recipes) or `images` (prebuilt, listed) |
| `config_via` | how its config is written, in the plugin's own word |
| `console` | `zephyr-shell`, or the name of a [dialect](#consoles-that-are-not-a-zephyr-shell) |
| `radio` | `managed` (RF rules enforced), `unmanaged` (warned), `none` |
| `update` | none (a core flasher writes the image, then its config), or a word naming `update_<word>.yml`, which does the whole of it |
| `locate` | its indicator (an LED, a buzzer), naming `locate_<word>.yml` |
| `plugin` | the key in `PLUGINS` of the object that answers for it |

## Two kinds of code

**On the controller**, the inventory plugin and tools import the plugin and call its `Family`
object. `core/plugins/plugin_utils/families/base.py` lists every hook with its generic answer:
`catalog`, `image` (what a `firmware:` declaration is), `groups` and `feature_groups`,
`board_row`, `board_facts`, `board_secrets`, `check_layout`, `rf_declared`, `rf_check`,
`survey_tags`, `survey_describe`, `enroll_note`, `enroll_hostvars`, `chip_regions`,
`release_partitions`.

**On a bench host**, a module is shipped only the code it imports by name, and a core module imports
no plugin. So what a core module needs of a family is data: `Family.spec(row)` turns the family into
a dict, published as `bench_families` and passed to modules as `families`
(`core/plugins/module_utils/familyspec.py` defines each key). A plugin's own modules may import
core's `module_utils` and their own.

| A `Family` attribute, as data | Used by |
|---|---|
| `state` (how its state line becomes an identity, with a fallback command), or `identity_probe` (a probe tool, or a console command whose prose answer is read, `lineless`) | every flasher, backup and erase: what the board runs |
| `image_match`, `image_bytes_check` | a flasher's preflight: is this the declared image |
| `config_regions` (`(role, flash map key)` pairs), `region_flashers` | region backup, restore and erase |
| `flash_regions`: for a chip with no table, `[{label, kind: boot\|app\|data, start, size, role?: config}]`, absolute; a catalog image's `regions:` wins | the write rule, and the region level of its `config` range |
| `reboot` (uptime, or a boot `banner` and `ready` command), `reset_command` | `just reboot`, reboot waits |
| `uf2_enter` (`shell_command`, `shell_usb`, `app_tools`) | the UF2 flasher asking an app for UF2 mode |
| `survey`: state line kinds, a prose command, or a probe `tool` in `host/bin/` run with the device's port (or, for kinds named in `probes` such as `swd-probe`, with `--probe <name> --wired <json>`), printing one JSON object | `just survey` |
| `step_tasks`: for a family with no console, the task file that runs one test step | `just scenario`, `just device-test` |

`release_partitions(member, read)` is a method: the other files of a release's zip that a full
install writes beside the merged ESP32 image `member`, each with its partition and md5. `just stage`
of an `esp-image` entry checks each md5 and lays them into the staged image.

## The state line

rxrbench's state-line protocol is one line a firmware prints in answer to the console command its
family names (`state_command`). Core reads it with no code of the plugin's. The zephyrtastic and
zephcore firmware print it (the mesh plugin's families); any firmware may.

    ~S{"v":1,"family":"widget","fw":"1.2.0+abc123"}*9a13

- `~S`, one JSON object, `*`, then four lowercase hex digits: CRC-16/XMODEM (Python's
  `binascii.crc_hqx(text, 0)`) of the JSON text exactly as printed. A line that is there but fails
  its checksum or is not JSON is refused, never read as "no state".
- Reserved keys: `v`, the line's version, checked against the family's `state` version when it gives
  one; `family`, which must name the family when its spec says `names_family`. Every other key is the
  family's: its `state` hook names the keys for target, version, build and its identity key.
- A family whose firmware cannot format a value may name placeholder tokens (`null_tokens`), read
  as null after the checksum.

A family with no state line is read by its `identity_probe`, or by a fallback command; with neither,
core says it cannot read the board. The parser is `core/plugins/module_utils/stateline.py`.

## Task files

Core's plays reach a plugin's `board` role with `include_role` and the task file the family names.
None is required; a play skips a step the family has no word for.

| Family says | Task file | Included by |
|---|---|---|
| `update: u` | `update_u.yml` | `just plan`/`apply`, instead of core's board role |
| `vocabulary: v` | `v.yml`; `facts_v.yml` | the board role after the image; `just facts` |
| `locate: l` | `locate_l.yml` (takes `locate_seconds`; 0 stops) | `just locate` |
| `role_tasks = t` | `family/t.yml` | the plugin's own config task file |
| `step_tasks = s` | `s.yml` (gets `step`, `step_name`; registers `board_step_said`) | `just scenario`, `just device-test` |
| `bootloader_tasks = {"uf2": "b"}` | `b.yml` | `just boot` |
| `chip_tasks = {"<flasher>": {...}}` | `backup`, `restore`, `erase`, `region_backup`, `region_restore`, `region_erase` | `just backup`, `restore-chip`, `erase-chip`, `restore-regions`, `erase` |
| `secrets` | `secret_<kind>[_<via>]_read.yml`, `..._write.yml` | `just secrets`, `keys-export`, `keys-restore` |

**Update.** An `update_u.yml` is the whole of bringing the device to its declared state. Its reading
step should return `device` (what the device runs, as core's flashers return it) and `image` (what
was declared): the state callback records any task result carrying them, and a board whose step
returns none is recorded as `not reported`.

**Chip tasks** are for a chip the plugin's own flasher writes, where core has no route. Each is
entered past core's gate, guards the device itself (`devreg guard`), and registers what core's
module would: `chip_backup` (`path`, `chip`, `files`), `restore` (`path`, `changed`, `msg`),
`erased` (`msg`); for regions, `backup` (`path`, `regions`), `restore` (`path`, `regions`,
`left_alone`), `erased` (`msg`). Its `FLASHERS` levels (`chip`, `regions`) let the plays include
them. `just erase` takes `region_erase` and only with a region named. `just erase-chip` takes `erase`
and first asks for a chip backup of the board on the workstation (`backups/<board>/chip/`) unless
`-e erase_without_backup=true`, since core cannot tell which chip a plugin's backup came from. A
plugin's module may run core's flow itself with its own `region_flow.Route`.

**Bootloaders.** `just boot` writes an ESP32's MCUboot itself. Any other bootloader's task file is
included after the gate with the board's image staged, is handed `boot_known_version`,
`boot_registry` and `boot_tags`, makes its own refusals, and registers `board_bootloader`
(`before`, `after`, `changed`, `why`).

**Argument specs.** Each task file is an entry point of the role, and `meta/argument_specs.yml`
declares what it reads: Ansible checks the types on entry, and `ansible-doc -t role
<namespace>.<name>.board` documents it. `tools/role_vars.py` finds every variable an entry point
reads; `tests/unit/test_role_specs.py` holds the specs to it both ways. Specs carry no `default`
(defaults stay in `defaults/main.yml`); a layout variable's type and text come from
`docs/layout-vars.yml`.

## Writing a flasher

A plugin's flasher subclasses `core/plugins/module_utils/flash/common.py::Flasher` and calls
`drive()`, which reads, compares, refuses, writes, verifies and reads again, marks the board busy,
and times the phases.

- **Formats.** Read the image with `common.load_image(image_dir, formats=("<format>",))` and refuse
  others in `preflight` with `common.common_refusals(manifest, FORMATS, ...)`. Pass `at_base=True`
  only if the flasher writes at the manifest's `base`.
- **The write rule.** A flasher that gives `read_range(start, length)` and `write_ranges()` is asked
  the write rule over the family's declared ranges with no code of its own.
- **Depth.** `drive()` hands the flasher `bench_check_depth` resolved as `checked`. A flasher lists
  reads with a spot form in `spot_reads` and the moments that force full in `depth_moments()`
  (`table_change`, `erase`, `no_full_record`). Listing `identity` lets a plan at `spot` use the
  board's last full read instead of `device()`: worth it only where `device()` is slow, and sound only
  for a board on a bench host's USB.
- **Timing.** A plugin's own module times phases with `module_utils/timing.py`
  (`with phases.phase("install"):`) and returns `timing=phases.out`; the `rxrbench.core.timing`
  callback records it.

## Secrets

A family lists the secrets its devices hold (`Family.secrets`), each with a kind and an origin:

- `device`: the device made it (a node's identity key, a host key). The site escrows a copy
  (`just keys-export`) and can give it back (`just keys-restore`).
- `declared`: the site is the authority (a channel key, a password, a certificate).

`secret_<kind>[_<via>]_read.yml` sets `board_secret`: `{id, fingerprint, value}` for a device
secret, `{id, declared, matches}` for a declared one, and `msg` when it could not be read. A device
secret also has `..._write.yml`, handed `board_escrowed` (`id`, `value`), which leaves a board that
already holds it alone. Core keeps the encrypted store (`<site>/identity/<board>.sops.yaml`), never
replaces an escrowed secret in silence, and shows no value. `Family.board_secrets(hostvars)` leaves
out what one board does not have; a layout opts one out with
`bench_secret_policy: {<kind>: {escrow: false}}`.

## Consoles that are not a Zephyr shell

The broker reads a Zephyr shell to a marker. A console that answers in one line is a **dialect**:
`host/dialects.d/<name>.json`, deployed with the plugin. Its keys:

| Key | Meaning |
|---|---|
| `reply` | a pattern; its first group that matched is the answer (it may span lines) |
| `secret_prefixes`, `log_scrub` | what the broker keeps out of its answers and log |
| `echo: false` | the console echoes nothing; the command is written paced |
| `eol` | the line ending it reads (`"\n"`) |
| `blank_reply` | what it answers to the blank line the broker sends first, waited for before the command |
| `prompts` | lines a client may have the broker answer once (`{"op": "attend", "dialect": ..., "ttl": 30}`) |

The family's `console` is the dialect's name, and its `state` and `reboot` say `dialect: <name>`.
`{"op": "reset"}` pulses the port's modem lines the way a flashing tool resets an ESP32. A plugin's
`module_utils` tells the broker client which commands carry a secret
(`broker_client.secret_shapes`), so an error never quotes one.

## RF rules

A site states its region. What a region allows a kind of radio (band, power) is a plugin's: its
`module_utils` calls `rfrules.register_radio("lora", {...})` on import, and core holds no numbers. A
family with `radio: managed` answers `rf_declared` (does the board transmit, in which region, at what
power, on which channel), and core applies the rules to that. `rf_check(name, hostvars, rules)` adds
what those numbers do not show: what the board's block makes another device transmit, or a property
of its channel such as a preset's width. A plugin's radio modules take the resolved rules
(`rf_rules`, from `bench_rf_rules`) and refuse the block in hand again at the write.

## Recipes

A plugin's recipes live in `<name>.just`, imported by the top-level `justfile` (an `import?` line).
Each needs a `[group("...")]` (one of those `tools/docs.py` orders) and a `[doc("...")]`:
`just docs-readme` writes the plugin README's recipes table between its `<!-- commands: ... -->`
markers, and a unit test fails when a recipe lacks either or a README is stale.

## The smallest example

`rxrbench.nuttx`:

- `plugins/plugin_utils/bench.py`: one row, `nuttx_nsh` (vocabulary `files`, console
  `zephyr-shell`, state command `uname -a`), and `PLUGINS`;
- `plugins/plugin_utils/families/nuttx_nsh.py`: how the state is read from `uname -a`, how it
  reboots, which secrets it holds;
- `roles/board/tasks/`: `files.yml`, `facts_files.yml`, the secret read steps;
- one module, `nuttx_files` (read, compare, gate, write), and `module_utils/nfiles.py`;
- `nuttx.just`: `sim-nuttx-build`, the simulated board's images.

## Tests a plugin should keep

- `tests/unit/test_families.py` holds every loaded family: every row has every field, and the spec
  survives being sent as JSON.
- `tests/unit/test_portability.py` holds core to never comparing a plugin's names and never
  importing one; `just core-only` runs core's tests with only the fixture plugin.
- `tests/unit/test_role_specs.py` holds each entry point's argument spec to what it reads.
- A plugin's own families should have a simulated board where they can
  ([SIMULATED.md](SIMULATED.md)); `tests/sim/` skips a bench until its images are built.
