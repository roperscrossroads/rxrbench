# rxrbench.mesh

LoRa mesh firmware as device families. A site loads it by naming it in `<site>/plugins.yml`
(`plugins: [rxrbench.mesh]`); the plugin contract it fills in is
[docs/PLUGINS.md](../../../../docs/PLUGINS.md).

| Family | What it is | Flash | Config |
|---|---|---|---|
| `zephyrtastic` | a Zephyr port of Meshtastic | built from `catalog/zephyrtastic.yml` | radio, identity key, owner, device, channels, admin keys, its own roles |
| `zephcore` | MeshCore-compatible firmware on Zephyr | a catalogued image | radio, identity key, name, adverts, admin password, storage epoch |
| `meshtastic` | stock Meshtastic | a catalogued image | the same `meshtastic:` block as zephyrtastic, through its phone API on its USB port or network API |
| `meshcore` | stock MeshCore | a catalogued image | name, adverts, `prefs`, admin password, identity key, and the radio when declared |

zephyrtastic runs on an ESP32-S3 (a Heltec V4) and an nRF52840 (a XIAO). Core knows the chips
and the flashers; the family knows the firmware; this plugin's catalogs say which chip a board
type has (`catalog/boards.yml`) and which Zephyr board and flasher each class is built for
(`catalog/zephyrtastic.yml`). An ESP32-S3 board has chip and region backups through esptool; a
XIAO, flashed by UF2, is backed up at the config level only.

| Path | What |
|---|---|
| `plugins/plugin_utils/bench.py`, `families/` | the families' rows and objects (what core loads) |
| `plugins/modules/` | `meshtastic_*`, `meshcore_*` (by vocabulary), `zephyrtastic_*` (that firmware's own features), `uf2_bootloader` |
| `plugins/module_utils/` | the vocabularies' state, plans and RF rules |
| `roles/board/tasks/` | the config, facts, key, locate and bootloader steps core's plays include |
| `host/` | for a bench host: tools (`bin/`), console rules (`dialects.d/`), its venv and a pinned bootloader update |
| `tools/` | staging a zephyrtastic build (`stage_image.py`, `partmap.py`) |
| `sim/` | this plugin's firmware on native_sim behind real brokers; `sim/stock/`, stock Meshtastic in a container |

## Declared state

A board declares its firmware, its family's own options in a block named after the family, and
its config in its vocabulary's block:

```yaml
# <site>/layouts/<layout>/host_vars/heltec1.yml
firmware: {family: zephyrtastic, class: 4, features: [brain]}   # built here from the catalog
zephyrtastic:                   # the family's block: brain, head, listener, cluster, intents, led
  brain: {heads: {xiao1: MEDIUM_FAST}}
meshtastic:                     # the vocabulary's block: the stock `--export-config` schema
  owner: heltec1
  config:
    lora: {region: US, modem_preset: SHORT_FAST, tx_power: 2, tx_enabled: true}
    device: {role: CLIENT, rebroadcast_mode: ALL}
    lora_api: {hop_limit: 3}    # LoRa fields only the phone API carries
    security:
      admin_key: ["base64:hSDwCYkwp1R0i33ctD73Wg2/Og0mOBr066SpjqqbTmo="]
  channels:
    - {index: 0, name: ShortFast, role: PRIMARY, psk: default}
```

```yaml
firmware: {family: zephcore, role: repeater, image: zephcore-sim-repeater}
meshcore:                       # MeshCore's CLI prefs
  name: zc1
  radio: {preset: usa, tx_dbm: 2, tx_enabled: true}     # or freq_mhz, bw_khz, sf, cr
  advert: {flood_interval_h: 12}
  prefs: {lat: 12.345679, lon: -45.678901, txdelay: 0.5}   # any other `get`/`set` key
storage:
  zephcore: {epoch: 1, keep_identity: true}
```

**Recipe features are part of an image's identity.** A brain and a plain member of one class
and build report the same class, version and build id; only the feature list tells them apart.

Layout variables this plugin reads: `bench_manage_channels` (`false` leaves every channel
alone), `board_led_mode` and `board_led_colors` (the bench LED), and core's
`bench_secret_policy` (`{identity: {escrow: false}}` leaves a node's key alone). Groups a
zephyrtastic board joins beyond the generic ones: `brains`, `heads`, `listeners`, `stock`,
`cluster_<name>`.

## What the layout checks add

- **Region.** A Meshtastic node's `lora.region` must be the site's; a MeshCore node's channel
  must sit in the region's band, under the power ceiling. `meshtastic_radio` and
  `meshcore_radio` check the block in hand against the rules again before writing.
- **No radio declared.** A board whose radio this tool sets and whose layout declares none is
  refused: a fresh node boots at its firmware's defaults (Meshtastic: LongFast at full power).
  Stock Meshtastic and MeshCore leave an undeclared radio alone, which a must-not-transmit
  board refuses.
- **The boot radio.** A stock MeshCore release transmits on its build's defaults before apply
  sets the radio. Its catalog entry records that (`boot_radio`): a first flash or a switch to
  it is refused where that breaks the site's rules, unless the board says `bench_boot_radio_ok`.
- **Preset width.** A zephyrtastic preset must fit the band, a brain's presets for its heads
  too.
- **Roles.** A brain and its heads name each other; a board with heads has the `brain`
  feature; `intents: none` declares no role beside it; `is_managed: true` needs an `admin_key`
  beside it.

## Secrets

| Kind | Origin | Families | How |
|---|---|---|---|
| `identity` | device | all four | a node's private key: escrowed, compared, given back to an erased or reflashed board |
| `channel_keys` | declared | zephyrtastic | `meshtastic.channels`, compared by fingerprint |
| `admin_keys` | declared | zephyrtastic | `meshtastic.config.security.admin_key`, compared as a set |
| `admin_password` | declared | zephcore, meshcore | compared by the fingerprint zephcore prints; stock MeshCore prints none, so it is written after a restore and never checked |

`just secrets <layout>` shows which are as they should be. A keyless zephyrtastic head holds
none.

### Admin keys and managed nodes

`meshtastic.config.security.admin_key` lists the public keys (at most 3) of the nodes that may
administer this one over the mesh; the node is given exactly that list, `[]` removes all, and
a layout that declares none leaves the node's own alone. `is_managed: true` makes the node take
configuration only from those admins, and **a managed node takes nothing from the bench**: the
firmware drops every local write, and only a remote admin or erasing its config region turns it
off. So `is_managed` is written last, by itself, after everything else has been read back, and
a managed board that differs from its layout is refused by name, in a plan too. Restoring a
node's identity key leaves its admin keys as they are.

## Stock Meshtastic

A stock node's USB port carries the phone API's binary framing, not a console, so it has no
broker (group `no_console`). `meshtastic_stock_config` opens the node itself, on its USB port
(resolved by name, guarded, locked) or on its network API at the registry's address.

- The radio goes first, as one LoRa config message checked against the RF rules. Then owner,
  device, hop limit, display and channels as one edit transaction, and security last. Every
  step is read back; writes are paced, since stock firmware drops admin messages sent back to
  back.
- The identity key is read, escrowed and given back (`meshtastic_identity`). A factory-reset
  node has no key until a region is set; `ensure` writes the escrowed one anyway.
- From 2.8 a node's id follows from its public key, so the escrowed key brings the old id back.
- A stock ESP32 release (`esp-image`) is written as the vendor's web flasher writes it. An
  update writes only the app partitions that differ and keeps config and key; a full install
  (a first flash, or `-e bench_full_erase=true`) writes the whole chip and leaves no config and
  no key (`apply`, then `keys-restore`). Its regions are `fs` (prefs, channels, the key) and
  `nvs` (Bluetooth bonds): `just backup`, `just restore-regions`, `just erase <layout> <board> fs`.
  On an nRF52 or RP2040 the release names no regions.
- What a stock board runs is read by the survey's probe (version and commit), so `apply`
  judges, flashes and switches a stock image like any other. A release is staged with
  `tools/stage_raw.py --version <v> --build <commit>`.

## The MeshCore vocabulary (zephcore, stock MeshCore)

A MeshCore console answers each command with one line, or a state line (`zephcore state`). The
broker reads it by `host/dialects.d/meshcore.json` and echoes a command carrying a secret only
up to its command word. Stock MeshCore says what it runs in its `ver` answer; its key is read
with `get prv.key` (kept out of the log) and given back with `set prv.key`. It has no
transmitter switch, so a declared `tx_enabled` is for the RF rules only.

- `meshcore_radio`: transmitter off, then power and parameters (applied by a reboot the module
  waits through), the transmitter on last and only when declared.
- `meshcore_config`: name, advert intervals, `prefs` (any other CLI key), the admin password.
- `meshcore_storage`: a declared storage epoch; another epoch on the node runs
  `zephcore wipe <epoch> keep|new` once, so a wipe is a state and apply stays idempotent.

## Bootloaders and bench-host tools

`just boot` takes a XIAO nRF52840's factory UF2 bootloader to OTAFIX, or to a rebuilt one a
layout declares (`bench_bootloader`: name, version, sha256, the file in the site's
`bootloaders/`), through the `uf2_bootloader` module and the gated `xiao_otafix.py`. Other
tools in `host/bin/`: `xiao_ble_dfu.py` (recovering an app-less XIAO over BLE DFU),
`imginfo.py` (an MCUboot image's header and TLVs), and the survey's read-only probes
`probe_meshtastic.py` and `probe_meshcore_companion.py`.

## Simulated benches

Layouts `sim` and `simswitch` run zephyrtastic on native_sim, `zcsim` two zephcore repeaters,
and `simmt` stock Meshtastic (meshtasticd in a container: `just sim-mt-up`, then `just plan
simmt`). Stock MeshCore has unit tests only. See [docs/SIMULATED.md](../../../../docs/SIMULATED.md).

## On hardware

| Family | Proven | Not yet |
|---|---|---|
| `zephyrtastic` | images by esptool (ESP32-S3), UF2 (nRF52840) and MCUmgr over BLE; radio, channels, admin keys and its own config; the identity key through a factory reset and back; facts, diag, reboot, device tests, a scenario on air | `locate` (the LED is not watched) |
| `zephcore` | the image by `apply` on an ESP32-S3; config and admin password; the key through an erase and back | MCUmgr over BLE |
| `meshtastic` | 2.7 and 2.8 releases; config over USB and the network API; the key after a factory reset; switching a board to it and back | its WiFi is set by hand |
| `meshcore` | its version; config; the key through a swap and a whole erase; a first flash refused for its boot radio; a scenario on air | checking the admin password; a companion build on hardware |

Details and quirks: [docs/HARDWARE.md](../../../../docs/HARDWARE.md).

## Recipes

What this plugin adds to the tool's `just` (its `mesh.just`, which the justfile imports).

<!-- commands: generated by `just docs-readme` from the recipes' [group] and [doc]; edit those -->
**Plan and apply**

| | |
|---|---|
| `just stage-image <build> <board_tag> [args ...]` | a zephyrtastic build into the image store (`manifest.json`, `flash.json` beside the image) |
| `just net-facts <host> [port=4403]` | read a stock Meshtastic node on the network (TCP 4403): owner, device, display, channels as fingerprints, admin keys; changes nothing |

**Simulated benches**

| | |
|---|---|
| `just sim-build [tree=main] [class=0]` | build the mesh firmware's sample app for native_sim from a firmware tree (catalog `sources:`) as fleet class `<class>`, and stage it |
| `just sim-build-zephcore [role=repeater]` | build zephcore's sample app for native_sim (role `repeater` or `companion`) from its workspace, and stage it |
| `just sim-up [boards ...]` | start the simulated mesh bench (layout `sim`): sim boards behind real brokers, in the background |
| `just sim-down` | stop the simulated mesh bench; the boards' config stays for the next `sim-up` |
| `just sim-mt-pull` | fetch the pinned meshtasticd image the simulated stock Meshtastic bench runs |
| `just sim-mt-up [nodes ...]` | start the simulated stock Meshtastic bench (layout `simmt`): meshtasticd in containers |
| `just sim-mt-down [args ...]` | stop the simulated stock Meshtastic bench (the nodes' config stays; `--erase` removes it, nodes may be named) |
| `just sim-mt-wipe <node>` | a simulated stock Meshtastic node comes back new: config and identity key gone |
<!-- commands: end -->
