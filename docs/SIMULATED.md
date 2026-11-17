# The simulated benches

A bench with no hardware: simulated boards behind real brokers, planned, applied and tested by
the same plays a real bench gets. They let a newcomer try the tool safely, let CI prove every
play on every change, and give a firmware project a bench before it has boards. Their layouts
are in `site.example/layouts/`, their registry and catalog in `site.example/sim/`.

## How a simulated board works

- **A board is a process.** A Zephyr application built for `native_sim` (or NuttX's sim, or a
  QEMU machine) runs as a process with its console on a pty and its flash in a file. The bench
  (`core/sim/simbench.py`) starts one process per board and a real broker for each, on a socket
  where a real board's broker would be.
- **Identity is the executable.** For a family with no state line (`bare`), what a board runs is
  the hash of the executable its process is, the way the SWD flasher hashes a chip's flash.
- **Flashing is a real flash, simplified.** Release the console, replace the file, restart the
  process, take the console back, read it again. The board's flash file is its whole chip, so
  `just backup`, `just restore-chip` and `just erase-chip` work on it; config regions are not
  modelled.
- **A board is `kind: sim`**: assumed present, with no USB for the plug watcher to report.

## The benches

| Layout | Boards | What it covers | Needs |
|---|---|---|---|
| `zsim` | `zb1`, `zb2`: Zephyr's shell sample (`bare`); `zb3`, `zb4`: `zapp`, the zephyr plugin's example app (`zephyr_settings`); `zb5`: Zephyr's SMP server (`zephyr_mcumgr`); `nb1`: NuttX's `sim:nsh` (`nuttx_nsh`); `zq1`: an emulated Arm MCU under MCUboot | plan, apply, flash to a second image and back, device tests, a scenario across boards; settings and secrets, key escrow and restore, upgrade and downgrade, backup and restore-chip; MCUmgr uploads; NuttX config as files; MCUboot's own choice (test, confirm, revert, a lower version refused) | a Zephyr workspace (`RXRBENCH_ZEPHYR_WORKSPACE`); for `nb1` a NuttX tree (`RXRBENCH_NUTTX_WORKSPACE`); for `zq1` the Zephyr SDK and `qemu-system-arm`; the MCUmgr venv |
| `zsim-vm` | the same boards | the whole of `zsim` inside a simulated bench host: a pinned Ubuntu cloud image in QEMU, reached over ssh like a Pi, given `hosts-install` and `hosts-setup` (the default install, devreg, the brokers, the plug watcher) | the `zsim` images, `qemu-system-x86`, working KVM |
| `simlinux` | `lin1`, `lin2` | the `linux` family: Linux devices as containers with an ssh server; re-imaging (a new host key) | Docker |
| `simmt` | `mt1` | stock Meshtastic: `meshtasticd` (its Linux build, pinned by digest) in a container, configured over its network API; a factory reset | Docker, the pulled image |
| `sim` | `sim1`, `sim2` | the mesh plugin's `zephyrtastic` on `native_sim`: radio, channels, secrets, a family of images | a zephyrtastic tree, built by `just sim-build` |
| `zcsim`, `simswitch` | `zc1`, `zc2`; `sim1` | `zephcore` on `native_sim`, and a board switched between zephyrtastic and zephcore | a zephcore workspace (not public) |
| `simab`, `simabrauc` | `ab1` | an A/B device in QEMU with RAUC slots, as `linux_slotguard` (with its health gate) and as `linux_rauc`: a signed bundle, the reboot, the slot read back, a rollback | a Yocto image of the device and a U-Boot sandbox binary (`simvm.py setup`), QEMU |
| `simabhawkbit` | `hb1` | the same A/B machine as a second device that takes its bundle through an update server (the hawkbit plugin's route): the server is told, the device's own client fetches, installs and reboots, the slot and its gate are read over ssh; a bundle that fails its gate is reported to the server and rolled back | the `simab` image; the server is a stand-in the test starts |

The emulated MCU (`zq1`) is Zephyr's SMP server under MCUboot on QEMU's MPS2 AN385, in MCUboot's
RAM-load mode with its slots in a simulated flash that survives a CPU reset. MCUboot there runs
the highest version it holds: an upgrade is its choice at the reset, a downgrade is never booted
(the flasher says so), and only a power cycle (the process started again) brings the factory
image back.

## What a machine needs

`just doctor` says what this machine has of what each bench needs, and what provides a missing
piece; `just doctor --require zephyr,nuttx` makes its exit code say whether those can run. Its
benches: `containers`, `zephyr`, `qemu`, `nuttx`, `smp` (the MCUmgr client venv), `vmhost`, `ab`
and `secrets` (sops and age, for the encrypted store the secrets tests use). Images a bench
builds are reported as built or not, never required.

The tool brings no Zephyr or NuttX tree: point `RXRBENCH_ZEPHYR_WORKSPACE` at any west workspace
that holds a Zephyr, and `RXRBENCH_NUTTX_WORKSPACE` at a directory with `nuttx/` and `apps/`
(NuttX's configure also wants `kconfiglib`). `west-zephyr-4.5.0.yml` and `west-zephyr-4.4.1.yml`
are manifests for such a workspace.

**KVM.** `/dev/kvm` can exist and not work (WSL2 without nested virtualization). Without KVM the
simulated host runs under TCG, where its setup outlasts a play's timeout, so its test skips.

## Running them

Build the images once:

```bash
just sim-zephyr-build      # zb1/zb2 shell images, zb5's signed SMP images
just sim-zapp-build        # zapp at 1.0, 1.1, 2.0
just sim-nuttx-build       # NuttX's sim at two builds
just sim-qemu-build        # zq, zq-b: the emulated MCU's images (Zephyr SDK)
just sim-build <tree> 0    # the mesh plugin's zephyrtastic sim image
```

Then run a bench and point the usual recipes at its layout, with `RXRBENCH_SITE=site.example`
when no site is set up:

```bash
just sim-zephyr-up                    # zsim, until stopped (Ctrl-C)
RXRBENCH_SITE=site.example just plan zsim
RXRBENCH_SITE=site.example just bench-test zsim      # apply, device tests, scenarios
```

| Bench | Up | Down | Other |
|---|---|---|---|
| `zsim` | `just sim-zephyr-up [boards]` | stop it | |
| `zsim-vm` | `just pcbench-up` (host in the background, default install, boards) | `just pcbench-down` | `just pcbench-test` (plan, apply, device tests, scenarios); `just sim-host-setup`, `sim-host-up`, `sim-host-ssh`, `sim-host-down` for the host alone |
| `sim` | `just sim-up` | `just sim-down` | |
| `simmt` | `just sim-mt-pull`, then `just sim-mt-up` | `just sim-mt-down [--erase]` | `just sim-mt-wipe <node>` |
| `simlinux` | `just sim-linux-up` | `just sim-linux-down` | `just sim-linux-reimage <device>` |
| `simab`, `simabrauc` | `just sim-vm-up` | `just sim-vm-down` | `just sim-vm-bundle`, `just sim-vm-sandbox` |
| `simabhawkbit` | started by its test (`tests/sim/test_hawkbit_sim.py`): `simvm.py --registry <the hawkbit plugin's sim/registry.yaml>` with `fresh hb1`, `up hb1`, `pin hb1` | stop it | `RXRBENCH_TEST_HAWKBIT=http://host:8080` runs the test against a real server |

`just test` runs the unit tests, every simulated bench whose images are built (the rest skip),
lint and the privacy check. The sim tests are in `tests/sim/`, one file per bench or feature;
each brings its bench up and down itself.

## What CI runs

`.github/workflows/test.yml`, on every push and pull request, in five jobs:

| Job | What it runs |
|---|---|
| `unit` | the unit tests, the same tests against core alone (`just core-only`), yamllint, ansible-lint, the privacy check, antsibull-docs' lint |
| `containers` | `simlinux` and `simmt` (Docker) |
| `mesh-sim` | zephyrtastic built for `native_sim` from its public tree at a pinned commit, then the `sim` tests |
| `sim` | a Zephyr workspace per release (4.5.0 and 4.4.1), the `zsim` images built, NuttX cloned at a pinned tag; the `zsim` tests, MCUmgr, NuttX, the emulated MCU (skipped on 4.4.1), core alone with a sim, run records, campaigns |
| `vm` | the `sim` job's images, QEMU, the cloud image (cached by its pin); the whole round trip on the simulated host |

`mesh-sim` and `sim` run as root inside a CI image, `ghcr.io/roperscrossroads/zephyr-arm`
(built from [roperscrossroads/images](https://github.com/roperscrossroads/images), pinned by
digest): the Zephyr SDK's Arm toolchain, the `native_sim`, QEMU and NuttX host tools and uv's
Python 3.12, so neither job installs a toolchain. `sim` also keeps its built images in the Actions
cache, keyed by what they are built from, and builds them again only when that changes.

In `containers` and `mesh-sim` a skipped test fails the job: a sim that did not run tested
nothing. `just ci [job]` runs a job and the jobs it needs locally with `act`, as GitHub does
(a clean clone of HEAD; a non-root user with sudo, or the job's own image).

Not in CI, run on a workstation: `zcsim` and `simswitch` (zephcore has no public source), and
`simab`/`simabrauc` (`tests/sim/test_rauc_sim.py`: the device image is a Yocto build of hours).

A second workflow, `.github/workflows/docs.yml`, builds the generated reference (`just docs`)
on each push to `main` and publishes it to GitHub Pages.

On a Forgejo instance, `.forgejo/workflows/test.yml` runs the `unit` job and the `sim` job (Zephyr
4.5.0) on a runner labelled `docker`, in the same image; the container sims and the VM need a Docker daemon and KVM,
so they run on GitHub only.

## What they cannot do

- **No radio between boards.** Each simulated board is an island; scenarios judge what each
  board says, not what they say to each other.
- **No USB.** The plug watcher, USB identity, hubs, 1200-baud touches and bootloader modes are
  not exercised; the survey of real hardware is not either.
- **No config regions.** A sim board's flash file is its whole chip; region backup and erase
  are not modelled.
- **No real flashers.** esptool, UF2, picoboot, SWD and BLE are not run against a simulated
  board; MCUmgr is (over the pty), and on `native_sim` nothing swaps the uploaded image in
  (there is no bootloader: the flasher says so). The swap is `zq1`'s.
- **No hardware timing.** A pass here says the plays are right, not that a real board behaves.
  What is proven on hardware: [`HARDWARE.md`](HARDWARE.md).
