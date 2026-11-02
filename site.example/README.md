# site.example — the shape of a site

rxrbench is the tool; a **site** is one bench's configuration. Yours lives in `site/` (or
wherever `RXRBENCH_SITE` points), which this repository ignores. Keep it as its own private git
repository: it holds your hostnames and your hardware. A layout is looked for in the site
first, then here, so every layout below works in a checkout with no site.

A site has the same shape: a `registry.yaml`, `rf_rules.yml`, `plugins.yml`, `catalog/`,
`layouts/` (with `hosts/`), and the generated `facts/`. Making a site from this one:
[`docs/GETTING-STARTED.md`](../docs/GETTING-STARTED.md).

## What it holds

| Path | What |
|---|---|
| `registry.yaml` | an example registry: a bench host, the workstation, a Heltec V4, a XIAO, a Linux device, an MCU and its debug probe |
| `rf_rules.yml`, `rf_rules.bench.yml` | the site's RF rules (`region: US`); the simulated benches' (low power, off the public preset) |
| `plugins.yml` | the bundled plugins, loaded |
| `catalog/` | example images and board types, among them the RP2040, RP2350 and KB2040 types a real site needs too |
| `sim/` | the registry and catalog the simulated Zephyr layouts bring with them |
| `tests/`, `campaigns/` | a library test bound in `zsim`, and a campaign that moves `zb1` to its image b and back (`just campaign zsim-images`) |
| `layers/sim`, `layers/simlinux` | what the simulated layouts share |

## Layouts

| Layout | What | Start it with |
|---|---|---|
| `example` | two real boards, `heltec1` and `xiao1`, as plain nodes: the one to copy for hardware | (needs a bench host) |
| `hosts` | the bench hosts, for `just survey`, `just hosts-install` and the plug relay | — |
| `sim` | two mesh boards on native_sim behind real serial brokers | `just sim-build`, `just sim-up` |
| `simswitch` | `sim`'s `sim1` declared as a zephcore repeater: a family switch and back | `just sim-build-zephcore` |
| `zcsim` | two zephcore repeaters on native_sim | `just sim-up zc1 zc2` |
| `simmt` | stock Meshtastic: `meshtasticd` in a container, over its network API | `just sim-mt-up` |
| `zsim` | Zephyr, NuttX and an emulated MCU under MCUboot | `just sim-zephyr-build`, `just sim-zephyr-up` |
| `zsim-vm` | `zsim` inside a simulated bench host (a VM) | `just pcbench-up` |
| `simlinux` | two Linux devices in containers, over ssh | `just sim-linux-up` |
| `simab`, `simabrauc` | a QEMU machine with A/B slots, with and without a health gate | `just sim-vm-up` |

What each simulated bench covers and what it needs: [`docs/SIMULATED.md`](../docs/SIMULATED.md).
