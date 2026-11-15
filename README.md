# rxrbench

**Alpha quality: use it, verify it, don't trust it yet** ("Status: alpha" below).

Declarative bench automation for embedded devices, on Ansible. A **layout** declares a bench:
which devices it holds, and what each one is (firmware, bootloader, radio config, channels,
roles). `just plan` is the plan (`--check --diff`), `just apply` the apply, and a second apply
changes nothing.

What the tool can do for a device depends on its **family**. Two are built in: any Zephyr
application with MCUmgr, and a chip behind a debug probe. The rest come from **plugins**,
Ansible collections a site names. Five ship here:

| Plugin | What it manages |
|---|---|
| [`rxrbench.mesh`](collections/ansible_collections/rxrbench/mesh/README.md) | LoRa mesh nodes on ESP32-S3 and nRF52840 (zephyrtastic, zephcore, stock Meshtastic, stock MeshCore), through a console or the network API |
| [`rxrbench.linux`](collections/ansible_collections/rxrbench/linux/README.md) | Linux devices over ssh: plain, with RAUC A/B slots, and with a health gate on top |
| [`rxrbench.zephyr`](collections/ansible_collections/rxrbench/zephyr/README.md) | any Zephyr application configured through Zephyr's settings shell, with its secrets |
| [`rxrbench.nuttx`](collections/ansible_collections/rxrbench/nuttx/README.md) | Apache NuttX devices through NSH, their configuration kept as files |
| [`rxrbench.launcher`](collections/ansible_collections/rxrbench/launcher/README.md) | ESP32 devices under the Launcher app manager: its version, the apps in its slots, its settings |

## Status: alpha

rxrbench is alpha software. It runs the bench it was built for, and it has rough edges and bugs. It
has been used on one bench, by one maintainer, with the boards listed under "Scope"; a bench that
differs will find what that one did not. It writes to hardware (flash, bootloaders, a chip's
whole image), so a bug can leave a board needing recovery by hand.

Use it, and don't trust it yet: verify it.

- **Start where nothing can break**: the simulated benches (below) need no hardware.
- **Read the plan before every apply.** `just plan` says what an apply would write, to which
  board, and why; an apply that does something the plan did not say is a bug.
- **Back up a chip before writing it** (`just backup`); `just restore-chip` puts it back.
- **Begin with a board that can be recovered** by hand (a ROM bootloader, a debug probe), not
  one a bench depends on.
- **Check the board, not only the run**: a play that says `ok` is a claim; the device's own
  answer is the proof.
- **Know what is proven**: "Proven on hardware, and not yet" (under "Scope") says what has run
  on real boards and what only on a simulated bench.

Layout keys, registry fields and recipe names may change between 0.x versions with no
migration. Bug reports are welcome as issues; anything that writes where it should not, or gets
past a refusal, goes to `SECURITY.md`. The licence (`LICENSE`) is MIT: no warranty.

## How it works

- **A layout is a directory.** It names the boards of a bench and declares each one's state;
  switching a bench to another topology is pointing at another directory. Layers hold what
  several layouts share.
- **Identity is not declared in a layout.** Which USB device is which board, and what must
  never be written, come from the device registry. Every write is checked against it on the
  host that makes it (`devreg guard`), and a layout only names devices the registry knows.
- **Only what is declared is managed.** A setting a layout does not mention is left alone.
- **Writes are planned first.** `apply` writes only where the layout says
  `bench_apply_allowed: true`; every other writing recipe is a plan unless `confirm=apply` is
  among its arguments.
- **It finds and identifies hardware.** `just survey` reads chip ids, MACs, flash and PSRAM,
  security eFuses, the bootloader and firmware of every USB device on a bench host, and a plug
  watcher reports boards as they come and go.

## Scope

Used on hardware so far: ESP32-S3 boards (Heltec V4, Vision Master E290), nRF52840 boards
(Heltec T114, Seeed XIAO), RP2040 and RP2350 boards, a Raspberry Pi 400 under RAUC, an
STM32F103 behind an ST-Link, and ESP32 boards under Launcher. Board types are data
(`catalog/boards.yml`, layered by each plugin and site).

### Proven on hardware, and not yet

What has run on real boards, what only on a simulated bench, and the hardware quirks a user
will hit: [`docs/HARDWARE.md`](docs/HARDWARE.md). Each plugin's README says what is proven for
its families.

A hardware CI runs the board exercise on a bench's real boards when `main` moves, and publishes
the newest result of each verb by board type and firmware (no board names) at
[rxrbench-results](https://roperscrossroads.github.io/rxrbench-results/); each commit it tested
carries the status `rxrbench/hardware`.

## Quick start, with no hardware

Install `just` 1.45.0, git, Python 3.12+ and PyYAML for the system `python3` first
([`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) has the details), then:

```bash
just setup                      # the venv, the collections, sops and age, the git hooks
just doctor                     # what this machine has of what the simulated benches need
just show example heltec1       # one board's declared state, each value with its source file
just sim-linux-up               # two simulated Linux devices in containers (Docker)
just plan simlinux              # what an apply would change
just apply simlinux             # do it; a second apply changes nothing
just device-test simlinux       # the layout's device tests
just sim-linux-down
```

From there to a real board (survey, enroll, declare, back up, stage, plan, apply):
[`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md). Every recipe: `just --list`, or
[`docs/COMMANDS.md`](docs/COMMANDS.md).

A bench's own configuration (its registry, layouts and facts) is a **site**, never committed
here: it lives in `site/` (ignored by this repository), usually a private git repository of its
own;
[`site.example/`](site.example/README.md) is a working one.

## Docs

| Doc | What it holds |
|---|---|
| [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) | requirements, setup, a simulated bench, a first real board, the refusals |
| [`docs/OVERVIEW.md`](docs/OVERVIEW.md) | the concepts: the words, a bench's life, the families, firmware |
| [`docs/LAYOUT.md`](docs/LAYOUT.md) | what a layout and its layers declare, the rules they must pass, every variable |
| [`docs/REGISTRY.md`](docs/REGISTRY.md) | the device registry and devreg (identity, guard, resolve) |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | how it is built: a plan and an apply traced, the bench host side, the safety rules |
| [`docs/PLUGINS.md`](docs/PLUGINS.md) | writing a plugin |
| [`docs/SIMULATED.md`](docs/SIMULATED.md) | the simulated benches and CI |
| [`docs/HARDWARE.md`](docs/HARDWARE.md) | what is proven on which hardware, and the quirks |
| [`docs/LOCATIONS.md`](docs/LOCATIONS.md) | where everything lives, and the environment variables |
| [`docs/COMMANDS.md`](docs/COMMANDS.md) | every recipe, generated |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | tests, the tree's rules, how a change is proposed |
| [`site.example/README.md`](site.example/README.md) | the shape of a site |
| plugin READMEs | [`core`](collections/ansible_collections/rxrbench/core/README.md), [`mesh`](collections/ansible_collections/rxrbench/mesh/README.md), [`linux`](collections/ansible_collections/rxrbench/linux/README.md), [`zephyr`](collections/ansible_collections/rxrbench/zephyr/README.md), [`nuttx`](collections/ansible_collections/rxrbench/nuttx/README.md), [`launcher`](collections/ansible_collections/rxrbench/launcher/README.md), [`devreg`](devreg/README.md) |

The modules, roles and Python API have a generated reference, built from the code on each push
to `main` and published at <https://roperscrossroads.github.io/rxrbench/> (`just docs` builds
the same locally, into `.build/docs`).

## Licence

MIT (`LICENSE`). Security reports: `SECURITY.md`.
