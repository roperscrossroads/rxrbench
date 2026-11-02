# Getting started

From a fresh checkout to a simulated bench, then to a first real board planned and applied.
`<host>` is a bench host, `<board>` the name you give a board, `<layout>` a layout of your site.
The words (layout, family, site, registry) are explained in [`OVERVIEW.md`](OVERVIEW.md).

## Requirements

### The workstation (where `just` runs)

| What | Why |
|---|---|
| Linux, or Windows with WSL2 | the recipes are bash; on WSL a board plugged into the workstation is attached through usbipd (`just wsl-attach`) |
| Python 3.12 or newer, with `venv` | `just setup` builds `venv/` from `requirements.txt` |
| `just` 1.45.0, `git`, `ssh` | the recipes, the hooks, the route to every bench host; distribution packages of `just` are older and refuse the justfile |
| PyYAML for the system `python3` (`python3-yaml`) | devreg runs under the system `python3`, as it does on a bench host |
| an age key | only for a layout with secrets (channel keys, passwords, escrowed identity keys); `sops` and `age` are pinned and installed by `just setup` |

`just` is the one tool installed by hand: the release archive for the machine's architecture,
checked against its sha256, into `~/.local/bin`.

```bash
v=1.45.0; a=$(uname -m)    # aarch64 or x86_64
curl -sSfLO https://github.com/casey/just/releases/download/$v/just-$v-$a-unknown-linux-musl.tar.gz
echo "<sha256 below>  just-$v-$a-unknown-linux-musl.tar.gz" | sha256sum -c -
tar -xzf just-$v-$a-unknown-linux-musl.tar.gz just && install -m 755 just ~/.local/bin/
```

| Architecture | sha256 |
|---|---|
| `aarch64` | `e9d629d3903ea2b6b2bbcb569e69d62bc80b0e6adcce280094c8ffb6dbdf83d2` |
| `x86_64` | `dc3f958aaf8c6506dd90426e9b03f86dd15e74a6467ee0e54929f750af3d9e49` |

Every other dependency is pinned by version and sha256 or an exact lock; `just pins list`
shows what this machine has of the pinned tools.

### A bench host (where boards are plugged in)

| What | Why |
|---|---|
| Debian, Raspberry Pi OS or Ubuntu | packages are installed with apt (elsewhere: `default_install_apt: []` and install them by hand) |
| `aarch64` or `x86_64` | the pinned binaries have a sha256 per architecture; on another, the install stops and says so |
| ssh from the workstation with a key, as an unprivileged user | every play, and the plug relay every 30 s |
| Python 3.9+ with PyYAML | Ansible's modules, devreg, the UF2 tools |
| systemd user services, with lingering | the serial brokers and the plug watcher are `systemctl --user` units |
| the user in the serial ports' group (`dialout` on Debian) | opening `/dev/ttyACM*` and `/dev/ttyUSB*` without root |
| `picotool`, for RP2040 and RP2350 boards | Debian 13 packages it; Ubuntu 24.04 does not, so install it by hand there |

Root is needed only for: the apt step of `just hosts-install` (`-K` asks for the sudo
password), enabling lingering in `just hosts-setup` (once), mounting a UF2 bootloader's drive
where the desktop does not (passwordless `sudo -n` for `mount`, `cp`, `umount`), writing an SD
card (`just card`), and setting a CP2102 bridge's serial (`just cp210x-serial`). Flashing an
ESP32, MCUmgr, a debug probe and every read run as the user.

What the flashers expect of a board: an ESP32 with its ROM download mode reachable over the same
USB cable, and secure boot and flash encryption off (the deep survey reads the eFuses; a
bootloader write or a first flash is refused without them known); an nRF52840 with the UF2
bootloader its board type requires (`just boot` writes it); a chip behind a debug probe with
the probe and the chip's unique id in the registry; a Linux device reachable over ssh.

### The simulated benches

Each simulated bench needs only the workstation, and some of the tools below. `just doctor`
checks them and names what provides a missing one.

| Bench | Needs |
|---|---|
| Linux devices and stock Meshtastic in containers (`sim-linux-up`, `sim-mt-up`) | Docker Engine usable by this user, `ssh-keygen` |
| mesh and Zephyr boards on native_sim (`sim-build`, `sim-zephyr-build`, `sim-zapp-build`) | a west workspace on Zephyr 4.5 (`RXRBENCH_ZEPHYR_WORKSPACE`), CMake 3.28+, `ninja`, `gperf`, `dtc`, a host `gcc` |
| the SMP flasher in the sim tests | `~/.venv-mcumgr` (made with `uv`: `just pins install uv` first) |
| the emulated MCU under MCUboot (`sim-qemu-build`) | the Zephyr SDK's Arm toolchain, `qemu-system-arm` |
| NuttX's sim board (`sim-nuttx-build`) | `nuttx/` and `apps/` side by side (`RXRBENCH_NUTTX_WORKSPACE`), `kconfiglib`, `make`, `gcc` |
| the simulated bench host (`pcbench-up`) | `qemu-system-x86_64` and usable KVM (without it the host is too slow and its test skips) |
| the A/B Linux device (`sim-vm-up`) | `qemu-system-arm`, a Yocto image built elsewhere, and U-Boot's sandbox (`just sim-vm-sandbox`) |

What each one covers: [`SIMULATED.md`](SIMULATED.md).

## Setup

```bash
just setup                      # the venv, the collections, sops and age, the git hooks
cp .env.example .env            # your settings; all optional
just doctor                     # what this machine has of what the simulated benches need
just test                       # unit tests, lint, the privacy check, and each simulated bench
                                # whose images are built (`just doctor` says which)
```

`just test` is the tool's own suite. It needs no hardware; a simulated bench whose images are
not built is skipped, and the VM bench needs KVM.

## A simulated bench

The layouts in `site.example/` work in a checkout with no site. These read only:

```bash
just show example heltec1       # one board's declared state, each value with the file it came from
just inventory example          # the layout as Ansible sees it
just layers zsim                # what a layout's layers give its boards
```

The cheapest bench you can write to is two Linux devices in containers:

```bash
just sim-linux-up               # layout `simlinux`: lin1 and lin2, ssh servers in Docker
just plan simlinux              # what an apply would change, board by board
just apply simlinux             # do it
just apply simlinux             # again: nothing changes
just device-test simlinux       # the layout's device tests
just sim-linux-down
```

Stock Meshtastic, configured through its network API the way a node on a network is:

```bash
just sim-mt-pull                # the pinned meshtasticd image
just sim-mt-up                  # layout `simmt`: one node, mt1
just plan simmt && just apply simmt
just sim-mt-down                # its config stays; `--erase` removes it
```

The mesh firmware on native_sim, behind the same serial brokers a bench host runs, needs a
west workspace: `just sim-build`, `just sim-up`, then `just plan sim` and `just apply sim`. The
other layouts are listed in [`site.example/README.md`](../site.example/README.md).

## Your site

A site is your bench's configuration: the registry, the layouts, the facts. It lives in `site/`
(or `RXRBENCH_SITE`), which this repository ignores; keep it as its own private git repository.

```bash
mkdir -p site/layouts
cp -r site.example/layouts/hosts site.example/layouts/example site/layouts/
cp -r site.example/catalog site/
cp site.example/registry.yaml site.example/rf_rules.yml site.example/plugins.yml site/
```

Copy `catalog/` too: the RP2040, RP2350 and KB2040 board types, and the keys that tell how
such a board is identified, live only there. Then:

- `site/layouts/hosts/hosts.yml`: your bench hosts and their user.
- `site/registry.yaml`: your hosts; remove the example boards.
- `site/rf_rules.yml`: your region.
- in a layout copied into the site, delete the `registry:` and `rf_ceiling:` lines of its
  `devreg.yml`: the site's are used.

A machine can be both workstation and bench host (a Raspberry Pi with the boards on it): name
it in both groups of `hosts.yml` with `ansible_connection: local`, and in the registry with
`local: true`. It does not build firmware: stage images built elsewhere. A Raspberry Pi 4
shares about 1.2 A across its USB ports, so several radio boards want a powered hub.

## The default install on a bench host

```bash
just hosts-install <host>       # -K when its sudo asks for a password
just hosts-setup hosts          # devreg and the registry on every bench host; lingering
```

`hosts-install` puts the same tools on every bench host and on the workstation; a second run
changes nothing.

| What | For |
|---|---|
| apt: `openocd`, `stlink-tools`, `dfu-util`, `stm32flash`, `bmap-tools`, `libusb-1.0-0`, `usbutils`, `python3-yaml`, `python3-venv`; `picotool` where packaged | debug probes, STM32 bootloaders, card writing, RP2040 |
| `uv`, `probe-rs`, `sops`, `age` | pinned release binaries in `~/.local/share/rxrbench/bin` |
| venvs `~/.venv-esp`, `-bench`, `-mcumgr`, `-swd`, and each plugin's (`-mesh`) | esptool, the plug watcher and host tools, MCUmgr, pyOCD, the plugin's modules |
| the plug watcher (bench hosts), the plug relay (workstations) | USB change notifications |

Each venv is synced exactly to its lockfile. Where everything lands: [`LOCATIONS.md`](LOCATIONS.md).

## A first real board

Every writing recipe but `apply` is a plan unless `confirm=apply` is among its arguments:
`boot`, `restore-regions`, `restore-chip`, `switch`, `keys-restore`, `erase` and `erase-chip`.
`apply` writes only where the layout allows it, and `just plan` is its plan. Start with a board
that can be recovered by hand.

### 1. Find it

```bash
just survey <host>              # every USB device there: ids, what firmware answers
just survey <host> true         # deep: also resets ESP32 boards to read chip, flash, PSRAM, eFuses
```

A board nobody has named is filed as `<site>/facts/new-<vid><pid>-<serial>.yaml`. Read it
before naming the board. An ESP32 behind a USB-UART bridge whose firmware the probes do not know
shows only as the bridge until the deep survey reads the chip. A port another tool holds is
skipped and reported as `held`.

### 2. Name it

```bash
just enroll <board> <serial> --layout <layout> --family <family> --key <value>
just enroll <board> <serial> --layout <layout> --family <family> --image <catalogued image>
```

The first form is for a family whose images are told apart by a key (a build class); the second
for stock firmware, declared by the image it runs. Both are checked against the catalog first.
`enroll` writes a registry entry matched by the board's own USB serial, files its facts under
its name, pushes the registry to the bench hosts, and adds the board to the layout. Then check:

- `policy.board_tag`: the board type. `enroll` sets it when the facts fit exactly one type, and
  otherwise lists the candidates for you to choose.
- `description`: what the board is, in your words.

A board behind a bridge with no serial of its own (a CH340) is matched by its chip: run the deep
survey first, and `enroll` records the MAC it read as `policy.chip_mac`. Every esptool write
reads the MAC first and refuses another chip. Two such boards on one host are ambiguous unless
something else tells them apart ([`REGISTRY.md`](REGISTRY.md)).

A board behind its built-in debug probe (a Nucleo's ST-Link) is enrolled as two entries, the
probe and the chip. Give either the probe's serial or the chip's unique id; the probe is named
`--probe-name`, by default `stlink-<board>`.

### 3. Declare it

`<site>/layouts/<layout>/` (copy `site.example/layouts/example`):

- `devreg.yml`: `plugin: rxrbench.core.devreg` and `boards: {<board>: {host: <host>}}`;
- `group_vars/all.yml`: `bench_apply_allowed: true` (nothing is written without it),
  `bench_store` and `bench_image` (where the staged image is), and
  `ansible_python_interpreter` (the bench host's venv);
- `host_vars/<board>.yml`: `firmware: {family: ..., image: ...}` (or `class:`) and the
  family's own block, described in the plugin's README.

`just show <layout> <board>` prints what the board resolves to and where each value came from.
Every key: [`LAYOUT.md`](LAYOUT.md).

### 4. Give the bench host what it needs

```bash
just hosts-setup <layout>       # the registry, and a serial broker for each board with a console
just facts <layout>             # what each board runs and how it is set; reads only
```

Run `hosts-setup` after every enroll and every change of a board's firmware family.

### 5. Back it up

```bash
just keys-export <layout>       # each node's own key, escrowed encrypted in the site
just backup <layout>            # config-region and whole-chip backups, where the board has them
```

`backup` reads at once; nothing is written to a board. A whole ESP32 chip behind a USB-UART
bridge takes minutes. Region backups hold the regions the running image's flash map names as
its config; chip backups cover ESP32, RP2040/RP2350 and probe-wired boards. The nRF52840 under
its UF2 bootloader has no chip or region backup: its drive never holds the config region. A
board with no backup path is named and the run refused; `-e partial=true` leaves it out.

### 6. Stage the image

A layout names an image; its bytes must be in the store (`bench_store`) with a
`manifest.json`. A published image in the catalog (`source: github` or `url`, pinned by sha256)
is fetched, checked and staged by:

```bash
just stage <image>              # --board-tag <type> when it is for several types
```

The catalog's `version` and `build` are what the firmware reports of itself: they tell a board
running the image from one that is not. A family that builds its own images stages them with its
plugin's recipe (`just stage-image`, `just stage-wic`).

### 7. Plan, then apply

```bash
just plan <layout>              # what would change, board by board; writes nothing
just apply <layout>             # the image if it differs, then the declared config
just apply <layout>             # again: nothing changes, so the board is as declared
just keys-restore <layout>      # a node that lost its key gets the escrowed one (plan; confirm=apply)
```

Read the plan before every apply. `just apply <name>` where `<name>` is only a layer says so and
stops; a layout of the same name wins over a layer.

### Another firmware on a board

Declare the other family in the board's host_vars, then `just hosts-setup <layout>` (its broker,
or none), `just boot <layout> confirm=apply` for a UF2 board, `just plan <layout>` (it says
`family switch: <old> -> <new>`), `just apply <layout>` and `just keys-restore <layout>`. A board
that answers as no family this host knows is a **first flash**, refused unless the board
declares `bench_first_flash: true`.

A radio firmware fresh on a board runs on its build's own radio until `apply` writes the
declared one. An image that transmits at once records `boot_radio` in its catalog entry; a fresh
write of it is then refused unless the board declares `bench_boot_radio_ok: true` (antenna on a
dummy load, or shielded).

### Erasing

```bash
just erase <layout> <board>             # lists the board's config regions; erases nothing
just erase <layout> <board> <region>    # the plan for one region; add confirm=apply to erase it
just erase-chip <layout> <board>        # the whole chip: a plan; confirm=apply erases
```

`erase-chip` checks the chip's identity in the same session, refuses a chip with no backup of
it on its bench host (unless `-e erase_without_backup=true`), and must read erased afterwards.
`just restore-regions` and `just restore-chip` put a backup back.

A board from another site is a new board here: survey, enroll and `just keys-export` it before
anything is written (a key escrowed only in the old site is encrypted for that site).

## When something says no

| It says | It means |
|---|---|
| `no broker at ...` | `just hosts-setup <layout>` has not run since the board was enrolled or its family changed |
| `no serial broker runs for it on its bench host` | the same, caught by `plan` or `apply` before anything is read |
| `the bench host's chip_blocks.py ... it is older than this checkout` | the host's tools predate this checkout: `just hosts-setup <layout>`. Nothing was written |
| `Overwriting it is a first flash` | the board runs nothing this host can read: check `just facts` before declaring `bench_first_flash: true` |
| `the board's bootloader version is not known: run just boot` | a UF2 board's bootloader has not been read yet |
| `layout <layout> does not set bench_apply_allowed` | the layout may be planned, not written to |
| `port busy: <holder>` | another tool has the board's port; its lock names who |
| `its first boot on this image would transmit before its config is written` | the image's `boot_radio` breaks the RF rules: write it only with the antenna on a dummy load, and say `bench_boot_radio_ok: true` |
| `not the board's USB port the bench reaches it by` | a picoboot image built without its USB console (Zephyr: `-S cdc-acm-console`); nothing would answer after the write |
| `the image has no MCUmgr over <transport>` | the build cannot be updated that way: another route, or a build that has it |
| `no backup path for ...` (or `no whole-chip path for ...`) `... Leave them out with -e partial=true` | a backup (or snapshot, or switch) would not cover those boards |
| `has no backup of this chip on its bench host` | `erase-chip` without a chip backup: `just backup <layout>` first, or `-e erase_without_backup=true` |
| `'<name>' is a layer, not a layout` | a layer has no boards; name a layout that inherits it |
| `no board answered, so nothing was planned` (or `applied`) | every board was skipped: not plugged in, or not answering |
