# rxrbench.linux

Embedded Linux devices reached over ssh. Nothing runs on a device but a shell and what its
image already has, so an image without Python is fine: every step runs on the host that can
reach the device and drives ssh itself. A site loads it in its `plugins.yml`; the plugin
contract it fills in is [docs/PLUGINS.md](../../../../docs/PLUGINS.md).

## Three tiers, three families

| Family | What it is | Update | What "healthy" means |
|---|---|---|---|
| `linux` | a Linux device | none: what it runs is read and compared with the declaration; changing it is re-imaging (`just card`) | nothing to gate |
| `linux_rauc` | with RAUC A/B slots | a signed bundle over ssh into the inactive slot, a reboot, the slot read back | RAUC's own `boot_status` |
| `linux_slotguard` | with RAUC and slotguard's health gate | as `linux_rauc` | the gate's unit: a slot is good only after its soak; a slot that never passes is failed over by the bootloader |

The two RAUC families add data to `linux`. A layout's own value beats the family's:

| Layout variable | Family default |
|---|---|
| `bench_rauc_remote_bundle` | none for `linux_rauc` (a layout that declares a bundle must say); `/data/update.raucb` for `linux_slotguard` |
| `bench_rauc_health_unit`, `bench_health_soak` | none; `boot-health-check.service` soaking 300 s for `linux_slotguard` |
| `bench_host_key_path` | the first present of the ed25519 and ECDSA keys under `/etc/ssh`; under `/data/ssh` for `linux_slotguard` |
| `bench_led_name`, `bench_led_path` | `ACT`, under `/sys/class/leds` |

## Declared state

```yaml
# <site>/layouts/<layout>/host_vars/linux1.yml
firmware: {family: linux_slotguard, image: node-image}   # an image of the site's firmware catalog
bench_bundle: /path/to/node-image.raucb                 # the signed bundle to bring it to
bench_wait_good: 600                                    # wait for the health gate (at least its soak)
```

```yaml
firmware: {family: linux, image: gateway}
linux:
  os: {id: poky, version_id: "5.0.4"}                   # /etc/os-release fields it must show
```

A device of any of the three families may hold **declared files**: small text files that are
the device's own to keep across updates (a client's config on its data partition), each
compared by SHA-256 and mode over ssh and written whole when it differs, before anything else
is done to the device. Their content is never read back and never in an output, so a file may
hold a secret. A file an image carries belongs in the image.

```yaml
linux:
  files:
    /data/app/config:
      mode: "600"                                       # the default
      content: |
        NAME=node-a
        KEY={{ node_a_key }}
```

A RAUC device may also take its bundle by a route that carries it to the device itself (an
update server the device polls): `routes: {flash: <bearer>}`, with that route in its registry
entry. The copy and the install are then the route's; the slot the device boots, and its
health gate, are read over ssh all the same, and decide whether the update worked.
[`rxrbench.hawkbit`](../hawkbit/README.md) brings such a route.

The device is a registry entry with an `ssh` route (`reach: {ssh: {host, ip, user, port,
state: live, can: [flash, shell, diag]}}`); the route's address is checked against the registry
(`devreg guard --route`) before any connection is made.

## What each command does

| Command | What happens |
|---|---|
| `just plan` / `just apply` | RAUC: the bundle against the booted slot, by `compatible`, version and hash; apply installs it into the other slot, reboots and reads the slot back (`bench_wait_good` also waits for the gate). Plain `linux`: what it runs against what is declared, nothing written |
| `just facts` | os-release, kernel, uptime; with RAUC, the booted slot and its health |
| `just secrets`, `keys-export`, `keys-restore` | the ssh host key as a secret the device made: escrowed, compared by its fingerprint, given back to a re-imaged device (`linux_host_key`) |
| `just locate` | one of its LEDs blinks for a while, then gets its trigger back |
| `just reboot` | over ssh, proven by its uptime |
| `just diag`, `just device-test` | over ssh; the family's default questions grow with the tier |
| `just card` | core's card writer; `just stage-wic` stages a `.wic` (reading the image's RAUC `compatible`) |

The layout check refuses a `linux_rauc` device that declares a bundle and not where it lands,
and a `bench_wait_good` shorter than the family's soak.

Not here yet: writing a device's configuration, updates delivered by a server, the bootloader.

## On hardware

`linux_slotguard` on a Raspberry Pi 400: a signed bundle into the other slot, the health gate
after its soak, and the host key escrowed. Plain `linux` and `linux_rauc`, and `just locate`,
run on the simulated benches only; `just card` has written no card yet. See
[docs/HARDWARE.md](../../../../docs/HARDWARE.md).

## Simulated benches

Two, described in [docs/SIMULATED.md](../../../../docs/SIMULATED.md):

- **Containers** (`just sim-linux-up`, layout `simlinux`): two devices with an ssh server and a
  BusyBox userland, no Python and no init system. They cover facts, plan, device tests,
  locate, reboot (proven by the connection dropping) and the host key; `just sim-linux-reimage`
  brings one back with a new host key. `tests/sim/test_linux_sim.py` needs Docker.
- **An A/B machine** (`just sim-vm-up`, layouts `simab` for `linux_slotguard` and `simabrauc`
  for `linux_rauc`): a qemuarm image built from `sim/vm/kas` on `meta-slotguard`'s own test
  harness, with RAUC slots, the health gate and ssh. The real boot script chooses the slot
  between boots, run by a U-Boot sandbox binary (`just sim-vm-sandbox`), so attempt counters,
  fall-back and mark-good are the real logic. `tests/sim/test_rauc_sim.py` covers a refused
  bundle, an update, the gate, an idempotent second apply, a reboot, and two broken bundles
  the boot script rolls back. `sim/vm/ARTIFACTS.yml` pins the image and sandbox it is known to
  run with.

## Parts

| Path | What |
|---|---|
| `plugins/plugin_utils/` | the three families (`bench.py`, `families/`) |
| `plugins/modules/` | `linux_facts`, `rauc_facts`, `rauc_install`, `linux_host_key` |
| `plugins/module_utils/` | `rauc.py` (the flasher and the health gate), `osinfo.py`, `hostkey.py` |
| `roles/board/tasks/` | the update, facts, secret and locate steps core's plays include |
| `tools/stage_wic.py` | a Yocto disk image into the image store, for the card writer |
| `sim/`, `linux.just` | the simulated benches and their recipes |

## Recipes

What this plugin adds to the tool's `just` (its `linux.just`, which the justfile imports).

<!-- commands: generated by `just docs-readme` from the recipes' [group] and [doc]; edit those -->
**Plan and apply**

| | |
|---|---|
| `just stage-wic <wic> <board_tag> [family=linux_rauc]` | a Yocto disk image (`.wic`, with its `.bmap`) into the image store, for `just card` |

**Simulated benches**

| | |
|---|---|
| `just sim-linux-up [devices ...]` | start the simulated Linux bench (layout `simlinux`): containers with an ssh server |
| `just sim-linux-down` | stop the simulated Linux bench |
| `just sim-linux-reimage <device>` | a simulated Linux device comes back new (another host key), as a re-imaged one does |
| `just sim-vm-up [devices ...]` | start the simulated A/B device: a QEMU machine with RAUC slots (layouts `simab`, `simabrauc`); needs `simvm.py setup` once |
| `just sim-vm-down` | stop the simulated A/B device |
| `just sim-vm-bundle [version=1.1-sim]` | sign an update bundle for the simulated A/B device from its built image |
| `just sim-vm-sandbox [args ...]` | build U-Boot's sandbox binary from mainline at the pinned tag (minutes), for `simvm.py setup --sandbox` |
<!-- commands: end -->
