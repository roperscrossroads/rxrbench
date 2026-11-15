# A layout and its layers

A layout is a directory under `<site>/layouts/` that declares one bench: which boards it has,
which bench host each is plugged into, and what each should run and be configured as. Ansible
reads it as an inventory through the `rxrbench.core.devreg` inventory plugin. `just plan <layout>`
says what applying it would change; `just apply <layout>` makes it so.
`site.example/layouts/example` is a small real one; `site.example/layouts/sim` a complete
simulated one.

```
<site>/layouts/<name>/
  devreg.yml                the boards, their hosts, the layers, an optional variant base
  group_vars/all.yml        what every board of the layout shares
  group_vars/all.sops.yaml  its secrets (SOPS; Ansible decrypts them at run time, the tool never does)
  host_vars/<board>.yml     one board's declared state
```

`devreg.yml`:

```yaml
plugin: rxrbench.core.devreg
layers: [base, nodes]            # optional, in order; a later layer wins
from: base                       # optional: this layout is a variant of another
boards:
  heltec1: {host: bench1}                    # registry name or alias, and its bench host
  xiao1: {host: bench1, profile: plain}      # its state from a layer's profile
rf_rules: {max_tx_power: 20}     # optional: may only tighten the site's rf_rules.yml
```

A layout inside a site uses the site's registry, catalog and RF ceiling. The keys `registry:`,
`catalog_dir:` and `rf_ceiling:` exist for layouts that bring their own (the example and the
simulated layouts, which must resolve in a checkout with no site); a site's layout leaves them out.

## Declared state, by example

```yaml
# <site>/layouts/<layout>/host_vars/<board>.yml
firmware: {family: <family>, class: 4, features: [<feature>]}   # or {family: <f>, image: <name>}
<family>:                       # the family's own block, where it has one
  ...
<vocabulary>:                   # its configuration, in the language its family speaks
  ...
```

What the two blocks hold is the family's plugin's to say (`rxrbench.mesh`'s README shows a mesh
node). The build a layout runs is declared once, in its `group_vars/all.yml` or a layer, so
rolling a new build is one edit.

## Rules a layout must pass

They are checked while the inventory is parsed. A layout that breaks any is refused as a whole,
with every problem listed:

- every board is in the device registry, on a bench host the registry knows;
- the site has said where it is (`region: US` in its `rf_rules.yml`) before anything transmits;
  no transmitter is declared for another region, outside the region's band, or above the most
  the region allows (or the less the site or layout asks for); a board whose radio the tool sets
  and which declares no radio config is refused;
- a board the registry says must never transmit is not given TX;
- what a family's own block makes possible across boards holds (its plugin checks it);
- the declared firmware exists in a catalog and is built for that board type.

The RF limits are data. None is built in but what a region gives (`US`, `EU`, `JP`: the band and
power the mesh firmware ships for it). A site states its region and may ask for less
(`site.example/rf_rules.yml`); a layout's `rf_rules` may only tighten that, so the file that
declares the transmitters cannot raise their limit. The module that writes a radio checks the
limits again on the config it is about to write, so a value given with `-e`, which the inventory
never sees, is refused too.

## Layers: what layouts share, declared once

Layouts of one bench repeat themselves: the build, the store, the interpreter, a bootloader,
whole board states that differ in one field. A layout may inherit those from **layers**
(`<site>/layers/<name>/`, then the tool's `site.example/layers/`):

```
layers/base/layer.conf                    required: `schema: 1`, a description, `depends: [...]`
layers/base/group_vars/all.yml            what every layout gets (store, interpreter, bootloader)
layers/nodes/group_vars/all.yml           one firmware family's build and signing key
layers/nodes/group_vars/family_<f>.yml    what boards of family <f> get; nothing for other families
layers/nodes/profiles/plain.yml           a named board state (firmware, radio, channels)
layers/roles/boards/heltec1.yml           an append: this layer's changes to one board
```

How a board's values are composed, from the bottom:

1. the layers' `group_vars/all.yml`, in order, **deep-merged** (dicts key by key; lists and
   scalars replaced);
2. the layers' `group_vars/family_<f>.yml` for a board whose `firmware.family` is `f`, the same
   way, so a layout may mix families;
3. the board's **profile**, merged across the layers that define it; a key the profile sets is
   the profile's whole, so a profile and a family file should not set the same key;
4. the **appends** (`boards/<board>.yml`, in layer order) and the board's `from:`
   (`<layout>/<board>`: that board's declaration in another layout, without that layout's
   group vars);
5. the layout's own `group_vars/all.yml` and `host_vars/<board>.yml`, which are Ansible's files
   and **replace a top-level key whole**.

So a layout that restates a block for one board replaces the profile's block entirely (the RF
rules say so if the radio config went with it), while an append in a layer changes one field
and keeps the rest.

Layer files (not the layout's own) take three operators: `key+:` appends to a list or merges
into a dict, `key-:` removes list items or dict keys, `key?:` sets a key only if nothing below did.

```yaml
# layers/roles/boards/heltec1.yml: keeps its profile, gains a test, loses a tool override
bench_tests+: [{name: uptime, cmd: kernel uptime}]
bench_tools-: [esptool]
```

A **variant** is a layout that is another with a few boards changed. It says `from: <layout>`:
every board of that layout it does not list comes along as declared there, on its host there.
Its layers, `group_vars` and go-aheads stay its own. A bench is then one complete layout
(`just apply base` puts everything back) and each variant is complete too while saying only what
it changes. A chain of variants resolves; a cycle is refused.

What layers may not do:

- state `bench_apply_allowed`, `bench_first_flash`, `bench_app_manager_overwrite` or
  `bench_overwrite`: go-aheads given by hand for one layout or board, and a layer that
  states one refuses every layout that uses it;
- hold secrets: `*.sops.yaml` stays in the layout;
- hold boards. `just apply <name>` where `<name>` is only a layer says so and stops (a layout of
  the same name wins);
- name another `schema`, or a `depends:` layer not listed before it: either refuses the layout.

`just layers <layout>` prints the stack; `just show <layout> <board>` prints every value with
the file that set it (`--ansible` checks that against what Ansible resolves);
`just inventory <layout>` prints the groups.

## The variables

The tables below are generated from `docs/layout-vars.yml`, each variable's one home. `just
docs-layout` writes them, and the same text into the `meta/argument_specs.yml` of each role that
declares the variable, where Ansible checks the type when a play enters the role
(`ansible-doc -t role rxrbench.core.board`). A unit test fails when either is stale. The plays
read a few directly (`bench_tests`, `bench_scenarios`, `bench_card_image`); no role declares those.

### What a layout sets (`group_vars/all.yml`, a layer, or a board's `host_vars`)

Go-aheads and per-board permissions (the four named above are never taken from a layer):

<!-- layout-vars: go-aheads -->
| Variable | Type | What |
|---|---|---|
| `bench_apply_allowed` | bool | `true` lets `just apply` (and every other writing play) write to this layout's boards; without it a layout can only be planned. Every writing play passes one gate (`playbooks/tasks/gate.yml`) and hands the go-ahead to each writing module (`apply_allowed`), which refuses without it |
| `bench_ci` | dict | per board: `{run: true}` lets a CI job exercise it (`tools/exercise.py <layout> <board> confirm=apply --ci`; `tools/ci_boards.py <layout>` lists the boards so marked), and `destructive: true` lets that run erase it and switch its family too, each undone within the run. A board without it is never touched by CI. Writing still needs `bench_apply_allowed` |
| `bench_first_flash` | bool | per board: `true` lets a board that does not answer as the image's family be written (an ESP32 is erased whole first) |
| `bench_uf2_enter_as` | str | per board, or `-e` for one run: the family whose way of asking for UF2 mode the board's app takes, when no family's read can say what it runs (a first flash). Needed only when the plan refuses because two families' shells run on the app's USB vendor and ask differently (two Zephyr firmwares on 2fe3). When the board says it runs another family, the flash refuses: what the board says wins (docs/ARCHITECTURE.md#entering-uf2-mode) |
| `bench_full_erase` | bool | for one run (`just apply <layout> -e bench_full_erase=true`): an `esp-image` board on its image's partition table gets a full install, the whole chip with every data partition as the image has it (its config and identity gone), instead of an update, which writes its app partitions only, as a vendor's web installer does without its full-erase box |
| `bench_boot_radio_ok` | bool | per board: `true` lets a fresh write (a first flash, a family switch) of an image whose catalog `boot_radio` breaks the RF rules go ahead: the board's antenna is on a dummy load, or it is shielded. Without it the flasher refuses, naming what the image would transmit on |
| `bench_console_elsewhere` | bool | per board: `true` lets a picoboot image whose shell is not on the board's USB port (the manifest's `console`, from its build) be written to a board the bench reaches by its console: its console is wired elsewhere (a UART on the bench host) |
| `bench_app_manager_overwrite` | bool | per board: `true` lets an ESP32 whose partition table shows an app manager (a firmware in its own partition that installs other images in slots beside it) be written at 0x0 or into those slots, which orphans every installed app: `bench_overwrite: [all]` for this board. Without it (or `bench_overwrite` naming each region the write takes) every ESP32 writer refuses such a chip: a plan by the write rule over the survey's record of the chip, the apply over the table and images it reads before writing |
| `bench_overwrite` | list | per board: the regions of the chip a write may take although they hold firmware the board does not run, by their partition labels (`[ota_1, game]`), or `[all]` (docs/ARCHITECTURE.md#partitions-and-the-write-rule). Without it, an ESP32 write that would cover such a region, or a new partition table that would drop one, is refused and names each region. On a chip with an app manager every data region (`nvs`, a filesystem) is shared by its apps, so a region erase or restore there names it too (`[nvs, spiffs]`). `bench_app_manager_overwrite: true` is `[all]` |
| `bench_foreign` | str | per layout or board: what a plan does with what the survey found on a chip that the layout does not own (other firmware in an app slot, data every app of a chip with an app manager shares; docs/ARCHITECTURE.md#partitions-and-the-write-rule): `info` (the default) prints it, `warn` prints it as a warning, `refuse` stops the board. Read from the survey's facts, with their date; a write checks the chip itself whatever this says. Regions `bench_overwrite` names are left out too, and what the board's family says its declaration owns there (a Launcher layout: Launcher, the apps it declares, and the shared data when it declares every app on the chip) |
<!-- layout-vars: end -->

Images and how they are written:

<!-- layout-vars: images -->
| Variable | Type | What |
|---|---|---|
| `bench_store` | str | the controller's image store root (a plugin's staging recipes fill it) |
| `bench_image` | str | the directory of a board's staged image (`manifest.json` beside the image); no image, no flash |
| `bench_flasher` | str | `esptool`, `esp-image`, `uf2`, `picoboot`, `smp`, `swd` or `sim` (a plugin may name its own): how the image is written. The catalog's class or the registry's `swd` route sets it, a layout may override |
| `bench_picoboot_enter` | list | per board, `picoboot` only: the ways into the ROM bootloader, in order, over the board type's `uf2.enter` (`reset-interface`, `touch-1200`, `shell:<command>`, `manual`) |
| `bench_picoboot_manual_wait` | int | per board, `picoboot` only: the seconds `manual` waits for BOOTSEL to be held while reset is pressed (default 0: not offered) |
| `bench_picoboot_enter_wait` | int | per board, `picoboot` only, over the board type's `uf2.enter_wait`: seconds to wait for the bootloader after each way in (default 10) |
| `bench_picoboot_ask_timeout` | int | per board, `picoboot` only, over the board type's `uf2.ask_timeout`: seconds a touch or shell command may take before that way counts as failed (default 15) |
| `bench_flash_unknown` | bool | `false` refuses a board that answers as its family but reports neither a build nor a hash, instead of writing it forward. Default `true`: firmware older than the build id could not be rolled otherwise. Whatever it says, the read-back after a write must match the image, and an image with no build or hash is always refused |
| `bench_check_depth` | str | `full` (the default) or `spot`, for a layout, a board or one run (`-e bench_check_depth=spot`): how much the checks before a write read (docs/ARCHITECTURE.md#checking-depth). `spot` reads only what a read's design says is enough for its decision, and only where a read has a spot form; a first flash, a family switch and the other moments that read in full ignore it. In a plan, a flasher that offers it (esp-image, smp) takes what the board runs from its last full read instead of asking it, while the bench host's plug watcher has seen nothing on its port since; a write by hand or over the air that makes no USB event is not seen (docs/ARCHITECTURE.md#checking-depth): keep `full` on a bench where hands or other tools write to boards. Each flasher's result says which depth it used and why (`checked`, `identity_checked`) |
| `bench_tools` | dict | where the bench host keeps a flasher's tools, when not where the default install puts them: a dict with any of `esptool`, `imginfo`, `uf2_tool`, `devreg`. UF2 boards are flashed through `~/rxrbench/bin/uf2_flash.py` |
| `bench_ble_rebond` | str | `smp` over BLE, what to do with a bond the host holds when the first exchange fails: `auto` (default) removes it only when the board, asked on its console, no longer holds it; `force` removes it without asking; `never` leaves it |
| `bench_sim_run` | str | the simulated bench's run directory (sockets, logs) |
| `bench_socket` | str | a board's broker socket path; the sim layer sets it, a real bench host's brokers are found by name |
| `bench_bootloader` | dict | the UF2 bootloader a layout wants (`name`, `version`, pinned by `sha256`): `just boot`, the mesh plugin's step |
| `bench_bootloader_policy` | str | when an ESP32 bootloader is rewritten: `key` (its signing key differs, the default) or `exact` (its bytes differ) |
| `bench_bootloader_carrier` | str | a staged image directory (like `bench_image`) small enough to carry a UF2 board through a bootloader update; a board whose installed image reaches the bootloader's staging area is refused without one |
| `bench_devreg_tool` | str | the devreg script on the host that reaches a Linux device, when it is not the deployed `~/.devreg/devreg.py` (a layout whose modules run on the controller names the repository's own) |
| a plugin's own |  | what its families read: `rxrbench.linux`'s README lists a Linux device's (`bench_bundle`, `bench_wait_good`, where a bundle lands, the unit that gates a slot), `rxrbench.mesh`'s a mesh node's |
| `bench_card_image` | str | the whole-disk image `just card` writes through a registered reader |
<!-- layout-vars: end -->

Tool options (tests, diagnostics, brokers, secrets):

<!-- layout-vars: tool-options -->
| Variable | Type | What |
|---|---|---|
| `bench_tests` | list | `just device-test`: a list of `{name, cmd, expect, refuse, rc, timeout}`; an empty list declares none (the board is named as untested, and a run where no board has any is refused) |
| `bench_scenarios` | list | `just scenario`: a list of `{name, steps}`, each step `{board, cmd, expect, refuse, rc, timeout, within, name}`: the command runs on the board named, judged there, with `within` seconds to come true; a failed step ends its scenario. Declared once for the layout (group_vars), since a scenario spans boards |
| `bench_test_bindings` | dict | `just scenario`, `just run`: the library tests this layout runs, `{<test>: {<role>: <board>}}`. A library test is `<site>/tests/<name>.yml` (the layout's own site): `needs.roles` says what each role's board must be (`families`, `via`, `features`), its steps name roles (`board: $a`). Each role is bound to a board here; a role left out is refused with the boards that would fit it, a board that misses a need with what it misses, and nothing runs until every binding holds. A bound test runs as a scenario of its name and its record carries the binding (docs/ARCHITECTURE.md#runs-tests-and-campaigns) |
| `bench_diag_commands` | list | `just diag`: what to collect beyond the vocabulary's defaults |
| `bench_diag_console_bytes` | int | `just diag`: how much of the console log to collect |
| `bench_broker_boards` | list | the boards a bench host runs serial brokers for (`just hosts-setup`); defaults to every board with a console on that host |
| `bench_locate` | str | per board: the indicator `just locate` uses, naming the plugin's `locate_<word>.yml`; the inventory publishes the family's own (`bench_led` is the family's word for it), and a layout sets another (a buzzer wired to the board) |
| `bench_secret_policy` | dict | `{<kind>: {escrow: false}}` leaves a secret a board made alone: not escrowed, not given back |
| `bench_host_*` |  | the bench_host role's knobs: deployed files, directories, links, pruning (`roles/bench_host/defaults`) |
<!-- layout-vars: end -->

### What the inventory publishes (read these; never build a path of your own)

<!-- layout-vars: published -->
| Variable | Type | What |
|---|---|---|
| `bench_board` | str | the board's name in the layout |
| `bench_devreg` | str | the board's name in the device registry |
| `bench_family` | str | the firmware family the board declares (`firmware.family`) |
| `bench_fw_kind` | str | what the registry says the board runs: `port`, `stock`, or `unknown` (the default) |
| `bench_plugin` | str | the plugin collection that brings the board's family (unset for a built-in one) |
| `bench_families` | dict | every loaded family as data (`module_utils/familyspec.py`): what a module on a bench host takes as `families` |
| `bench_update` | str | how the family is updated: none for this tool's flashers, else its plugin's step (`update_<word>.yml`) |
| `bench_vocabulary` | str | the family's configuration language, which names its plugin's task file |
| `bench_config_via` | str | how the family's configuration is written (a console, a phone API tunnel, the network) |
| `bench_identity_key` | str | what names an image of the family (the key of its identity a catalog image records) |
| `bench_board_tag` | str | the registry's board type for the board: an image must be built for it |
| `bench_compatible` | str | the registry's `compatible` for the device: an update bundle must carry it |
| `bench_may_transmit` | bool | the registry's word on whether the board may transmit at all |
| `bench_usb_serial` | str | the registry's `match.usb_serial`: the chip a flasher that selects by it is handed (`picoboot`) |
| `bench_chip_mac` | str | an ESP32's MAC as the registry holds it (`policy.chip_mac`, else a `usb_serial` that is a MAC, else the MAC a `serial_by_id` carries): what `esp-image`, the esptool routes and `erase-chip` check in their loader session before they write |
| `bench_rf_rules` | dict | the RF rules this board was checked against: the site's ceiling, tightened by the layout's `rf_rules`; handed to the modules that write a radio, which check again what they are about to write |
| `bench_mcu` | str | the board type's chip (`catalog/boards.yml`: core's, each plugin's and a site's, later over earlier): what esptool is told |
| `bench_psram` | raw | the board type's PSRAM, which a first flash checks |
| `bench_routes` | dict | one route per verb (`shell`, `flash`, `config`, `diag`): bearer, host, address, what it can do |
| `bench_access_host` | str | the bench host the board is reached from |
| `bench_present` | bool | whether the board is taken to be there: the plug watcher sees it, or could not tell (no fresh index, a simulated board, a chip behind a debug probe, a board the registry gives no chip id or MAC: assumed present, the last with a warning, and its first read decides). A board the watcher would know and does not see is skipped, and so is one whose first read finds nothing there |
| `bench_via` | str | how a command reaches the device, from the route chosen for its shell: `console` (through its serial broker), `ssh`, or none. `just diag` and `just device-test` use it; a device with no console is in the `no_console` group and gets no broker |
| `bench_can` | list | what can be done to a device flashed its way beside writing its image: `regions` (backup, restore and erase of its config regions), `chip` (chip backup and restore). Empty for a flasher that can do neither; the plays skip or refuse by this, never by a flasher's name |
| `bench_flash_regions` | list | from the catalog image's `regions`, else its family's `flash_regions`: the ranges of a chip with no partition table and no flash map in its images (`[{label, kind, start, size, role}]`), checked when the layout loads. A flasher that can read the chip asks the write rule over them, and refuses a write over the `boot` range unless `bench_overwrite` names it (docs/ARCHITECTURE.md#partitions-and-the-write-rule) |
| `bench_sources` | dict | from the catalog: where each family's source tree is on this machine |
| `bench_secret_kinds` | list | the secrets the board holds, each with its origin, its task files and whether the site escrows it |
| `bench_key_escrow` | str | how the board's identity key is escrowed, for a family's own tasks |
| `bench_family_tasks` | str | the family's own task file within its plugin's vocabulary |
| `bench_diag_defaults` | list | what `just diag` asks the family by default |
| `bench_family_facts` | dict | what the family's task files need to know of this board, each also as `bench_<key>` (what they are is its plugin's README's to list) |
| `bench_repo` | str | the tool's repository (its `venv/`, `tools/`, `runs/`, `backups/`, `.build/`): a layout names a path under it with this, never with `playbook_dir` |
| `bench_site_dir` | str | where the site is (`module_utils/site.py`) |
| `bench_registry` | str | the site's device registry file |
| `bench_facts_dir` | str | where the site keeps the boards' facts (what the survey and `just boot` record) |
| `bench_layout` | str | the layout's name |
| `bench_layout_dir` | str | the layout's directory |
| `bench_layers` | list | the layers the layout inherits, lowest first |
<!-- layout-vars: end -->

Groups: `boards`, `bench_hosts`, `host_<host>`, `kind_<kind>`, `family_<family>`,
`<identity_key>_<value>` (`class_4`, `role_repeater`), `profile_<name>`, `absent`, `no_console`,
`stock`, and the family's own (its plugin's `groups`).
