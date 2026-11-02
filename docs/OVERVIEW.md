# rxrbench, as a whole

The concepts in one read: what the tool is, the words it uses, a bench's life under it, what is
always true, the device families, where firmware comes from, and Launcher boards in brief. How
the code carries it out is [`ARCHITECTURE.md`](ARCHITECTURE.md); a first run is
[`GETTING-STARTED.md`](GETTING-STARTED.md).

## What it is

rxrbench brings a bench of embedded devices to a declared state, with Ansible. A directory (a
**layout**) says which devices are on the bench and what each one should be: its firmware, its
bootloader, its radio config, its other config, its secrets. `just plan <layout>` says what
differs on every board; `just apply <layout>` makes the boards match; a second apply changes
nothing. Every step reads the device, changes only what differs, and reads it again: success is
what the device says afterwards, never what a tool printed.

It is meant for a bench that outlives any one firmware project. The same plays flash a Zephyr
mesh node over USB, update a Raspberry Pi's A/B slot over ssh, configure a NuttX board through
its shell and install an app on an ESP32 running an app manager. What the tool can do for a
device is its **family**'s to say, and families come from plugins. The framework holds no one
firmware's or board's knowledge and imports no plugin.

## The idea

- **A layout is a directory that declares a whole bench.** Switching topology is pointing at
  another directory.
- **Identity is not declared in a layout.** Which USB device is which board, what it is and
  what must never be written come from the device registry. A write is checked against it on
  the host that makes it: a USB path with `devreg guard`, a Bluetooth or network address with
  `devreg guard --route` (it must be the one the registry records), a debug probe by the chip id
  it reads. A layout only names devices the registry knows.
- **Only what is declared is managed.** A setting a layout does not mention is left alone.

## The words

| Word | What it means here |
|---|---|
| **tool** / **site** | The tool is this repository. A site is one bench's configuration (layouts, registry, facts, hosts, catalog, secrets) in `site/`, kept as its own private repository. `site.example/` is the shape |
| **workstation** / **bench host** | The workstation runs `just` and Ansible. A bench host is a machine boards are plugged into, reached over ssh; the workstation can be one too |
| **registry** / **devreg** | The registry is the hand-kept identity of every device: name, how it is matched, routes, policy ([`REGISTRY.md`](REGISTRY.md)). devreg is the gate over it: `resolve` (absent: rc 1, ambiguous: rc 2, never a guess) and `guard` before every write |
| **facts** | What `just survey` found of a device (chip, ids, flash, PSRAM, eFuses, bootloader, firmware, app manager), with capability tags, kept per device in the site |
| **layout** / **layer** | A layout is a whole bench's declared state. Layers are what layouts share, declared once and inherited in order ([`LAYOUT.md`](LAYOUT.md)) |
| **catalog** | Board types by the registry's `board_tag` (core's, each plugin's, the site's) and a site's prebuilt images, each with its source and a sha256 pin |
| **family** / **plugin** | A family is the firmware or operating system a device runs. A plugin is an Ansible collection that brings families and the task files that configure them ([`PLUGINS.md`](PLUGINS.md)) |
| **broker** / **dialect** | One process per board console on its bench host, owning the port; every reader and writer goes through it, and secrets stay out of its log. A dialect is the rules for a console that is not a Zephyr shell |
| **route** | How a device is reached for a verb (`flash`, `shell`, `config`, `diag`): USB, ssh, SWD, BLE, wifi, Ethernet. One per verb, chosen when the inventory is built, with no fallback mid-run |
| **state line** | `~S{json}*crc16`, printed by a firmware for one console command: target, version, build and identity key, read with no code of the firmware's own. An image and a device have the same shape of identity, so "needs flashing" is a comparison |
| **plan** / **apply** | The plan is `--check --diff`: what differs, nothing written, no board reset. The apply writes one board at a time, stops the bench on a failure, and reads every write back |
| **go-aheads** | `bench_apply_allowed` (anything may be written), `bench_first_flash` (a board may be erased and written whole), `bench_app_manager_overwrite` (an app manager's apps may be orphaned): a layout states them itself, a layer never |
| **class** | What a family tells its builds apart by (`firmware: {family: zephyrtastic, class: 4}`); the catalog resolves it to an image. Other families name the image (`image:`) |
| **check depth** | `bench_check_depth`: `full` (the default) reads everything before a write; `spot` only what is enough |
| **plug watcher** | Keeps each bench host's USB snapshot; the workstation's relay posts plug and unplug notices to ntfy and runs the passive survey. A board it does not see is skipped as not plugged in |

## A bench's life

1. **A host.** `just hosts-install <host>` gives it the default install: apt packages, pinned
   binaries, one venv per tool family synced to a lockfile, the plug watcher.
   `just hosts-setup <layout>` adds devreg, the registry and a broker for every board of the
   layout on that host.
2. **A board.** Plug it in. The relay posts a notice and runs the passive survey;
   `just survey <host> [deep]` identifies every USB device and writes its facts;
   `just enroll <name> <id>` turns the facts into a registry entry (and, asked, a layout entry).
   A board nothing matches is kept as `new-*` facts until it is registered or unplugged.
3. **A declaration.** The board's `host_vars` in a layout: `firmware:` (its family and either a
   class the family builds or an image the catalog lists), the family's own block, its
   vocabulary's block, its secrets in SOPS. `just show <layout> <board>` prints every resolved
   value with the file that set it.
4. **The plan.** `just plan <layout>`. The inventory refuses a layout that breaks a rule (every
   board registered, the site's RF rules, every image catalogued and built for that board type),
   listing every problem; then each board is read and the difference listed. Where a read would
   reset a board, the plan decides from what was last read, defers to the apply, or says it
   cannot know.
5. **The apply.** `just apply <layout>`, one board at a time; a write that went wrong is never
   followed by the next board's. Bootloaders have their own recipe (`just boot`). The recipes
   that write outside the apply (`boot`, `restore-regions`, `restore-chip`, `switch`,
   `keys-restore`, `erase`, `erase-chip`) are plans unless `confirm=apply` is an argument.
6. **Reading back.** `just facts`, `just diag`, `just secrets` (whether each device holds the
   secret it should, never showing it), `just device-test` and `just scenario` (steps across
   devices, each judged on the board it names), `just bench-test` for all of it, and `just run`
   for the tests with a record of the state they ran against. Runs land in `runs/<time>/`.

Backups sit beside this. `just backup` reads config-region backups and whole-chip backups of a
layout's ESP32, RP2040/RP2350 and probe-wired boards; `just snapshot` names a whole-chip state of
its ESP32 and RP2040/RP2350 boards and `just switch` returns to it. The nRF52840 behind its UF2
bootloader has neither kind of backup: its drive never exposes the region its config lives in.

## What is always true

- No write without `devreg guard` on the very path, address or chip id about to be written, on
  the host that writes. Ambiguity is a refusal. One owner per port: the broker holds the device
  lock, and a flash releases it for the tool and takes it back.
- No write without the layout's `bench_apply_allowed`, checked by the play and again inside every
  writing module, so a module run outside a layout's apply refuses too.
- Nothing transmits until the site has said where it is. A board declared for another region,
  outside the band or above the allowed power is refused before the layout is accepted and again
  where the radio is written. A site may ask for less than its region allows; a layout may only
  tighten that.
- An ESP32 whose partition table shows an app manager is not written over it (at 0x0, its app
  slot, its bootloader or its config regions) unless its layout says
  `bench_app_manager_overwrite`, because the write orphans every installed app.
- A whole-chip erase checks the chip's identity in the same session, is refused without a chip
  backup of that board unless the run says `-e erase_without_backup=true`, and must read erased
  afterwards.
- A secret (a channel key, a password, a device's own key) never reaches a log, an argv or a
  commit: SOPS in the site, fingerprints in the output, the broker told what to scrub.
- A plan resets no board. A Bluetooth bond is removed only on the board's word.

## The families

What the tool does for a device depends on its family:

| Family | Flash | Config | RF rules |
|---|---|---|---|
| `zephyr_mcumgr` (built in: any Zephyr app with MCUmgr) | MCUmgr over serial, BLE or UDP | none | no radio |
| `bare` (built in: a chip behind a debug probe, or a simulated board) | SWD (or the sim) | none | no radio |
| a plugin's | its flasher: esptool, UF2, picoboot, MCUmgr, ... | what its task files bring to the declared state | enforced where the plugin says what its radio declares |

Five plugins ship in this repository:

| Plugin | Families | What it manages |
|---|---|---|
| [`rxrbench.mesh`](../collections/ansible_collections/rxrbench/mesh/README.md) | `zephyrtastic`, `zephcore`, `meshtastic`, `meshcore` | LoRa mesh nodes on ESP32-S3 and nRF52840: image, radio, channels, owner, identity key (escrowed and given back), each firmware's own config; stock Meshtastic over its serial port or network API, stock MeshCore on its text console. The RF rules live here, an image's own boot radio included |
| [`rxrbench.linux`](../collections/ansible_collections/rxrbench/linux/README.md) | `linux`, `linux_rauc`, `linux_slotguard` | Linux devices over ssh: read and compared; a signed RAUC bundle into the inactive A/B slot and the slot read back; the same under a health gate, where a slot is good only after its soak. Nothing runs on the device but a shell |
| [`rxrbench.zephyr`](../collections/ansible_collections/rxrbench/zephyr/README.md) | `zephyr_settings` | Any Zephyr application that takes Zephyr's settings shell: declared settings written one by one, secrets by kind (a device-made key escrowed, a declared PSK or certificate compared) |
| [`rxrbench.nuttx`](../collections/ansible_collections/rxrbench/nuttx/README.md) | `nuttx_nsh` | Apache NuttX through NSH: known by `uname -a`, its configuration as files on storage mounted first |
| [`rxrbench.launcher`](../collections/ansible_collections/rxrbench/launcher/README.md) | `launcher` | ESP32 boards under the Launcher app manager: its version, the apps in its slots and its settings, through its serial console (below) |

A firmware of a family no loaded plugin knows is **foreign**: it can be catalogued, pinned by
sha256 and checked against the board type, and the board is warned about. It is not flashed,
since nothing could tell what the board runs afterwards, and since the tool cannot set its power
or frequency.

Board types are data (`catalog/boards.yml`: core's, then each plugin's, then a site's own).
Boards used so far: Heltec WiFi LoRa 32 V4 and Vision Master E290 (ESP32-S3), Heltec T114 and
Seeed XIAO nRF52840, Raspberry Pi Pico 2 W, SparkFun Pro Micro RP2040 and Adafruit KB2040, a
Raspberry Pi 400 (RAUC), an STM32F103 behind an ST-Link, a NUCLEO-F767ZI on Ethernet, a CYD
2432S028, and a LilyGo T-Embed under Launcher. What is proven on which:
[`HARDWARE.md`](HARDWARE.md).

## Firmware: built here, or anyone's

```yaml
firmware: {family: <family>, class: 4, features: [<feature>]}   # built here from the family's catalog
firmware: {family: <family>, image: sensor-node-1.4}            # listed in <site>/catalog/firmware.yml
```

A listed image names where its bytes come from: a local path, a URL, a GitHub release, a
Forgejo release or package. Anything remote is pinned by sha256 and cached by it. An image built
elsewhere as an Intel HEX or ELF file is staged as a binary with the address it goes at.

A flasher tells what a board runs through its family's state command. An image whose manifest
carries neither a build id nor a hash is refused, since nothing could tell whether a board runs
it. A board that answers as its family but reports neither is written forward unless the layout
says `bench_flash_unknown: false`.

What a firmware cannot do, the tool says rather than works around. Zephyr's settings shell reads
back 256 bytes of a value at most; a firmware that prints a setting's digest
(`zephyr.digest_command`) lifts that. An image that transmits before its config is written is
declared with `boot_radio` in its catalog entry; where that radio breaks the site's RF rules, a
fresh write is refused unless the board declares `bench_boot_radio_ok: true` (its antenna on a
dummy load). A firmware's own bug shows as a failed step ("written, but reads back as
something else"), not a quiet success.

## Launcher boards, in brief

[Launcher](https://github.com/bmorcelli/Launcher) is an app manager for ESP32 boards: it sits in
the ESP-IDF `test` partition at 0x10000 and installs other images beside it, one OTA slot each.
A board under it runs Launcher, holds several images, and boots one. The `launcher` family
exists for that shape; [its README](../collections/ansible_collections/rxrbench/launcher/README.md)
has the detail.

- **Declared:** a Launcher release as the firmware, and a `launcher:` block: `apps` (slot label
  to catalog image), `boot`, Launcher's own settings, saved `wifi` networks, `prune` (off by
  default).
- **Read:** through its serial console, which exists only while Launcher runs. From a running app
  the family gets back to Launcher by a power-on through the port's modem lines; a board whose
  port cannot power it on is read only when someone has put it in Launcher, and the plan says so.
- **Planned:** a board in its app is deferred to the apply. App bytes cannot be read back, so a
  slot is known by its label, its size and the install record kept in the board's facts.
- **Written:** Launcher's version, settings, networks, then apps, the boot slot last (an install
  selects what it wrote).
- **Guarded:** a first install of Launcher needs `bench_first_flash: true`; writing over it needs
  `bench_app_manager_overwrite`; the whole chip is erased only by `just erase-chip`.

No install has completed on real hardware yet; a site declares such boards with
`bench_apply_allowed: false` until one has.

## Where to read next

| | |
|---|---|
| [`GETTING-STARTED.md`](GETTING-STARTED.md) | requirements, setup, a simulated bench, a first real board |
| [`LAYOUT.md`](LAYOUT.md) | what a layout declares, the rules it must pass, layers, every variable |
| [`REGISTRY.md`](REGISTRY.md) | the registry and devreg |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | how a plan and an apply move through the code, the bench host side, the safety rules in code |
| [`PLUGINS.md`](PLUGINS.md) | writing a plugin |
| [`SIMULATED.md`](SIMULATED.md) | the simulated benches and CI |
| [`HARDWARE.md`](HARDWARE.md) | what is proven on hardware, and the quirks a bench will hit |
| [`LOCATIONS.md`](LOCATIONS.md) | where everything lives |
| [`COMMANDS.md`](COMMANDS.md) | every recipe |
