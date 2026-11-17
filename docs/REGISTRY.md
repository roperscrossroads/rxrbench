# The device registry and devreg

The registry (`<site>/registry.yaml`, or `DEVREG_REGISTRY`) is the site's hand-kept identity of
every device. `devreg` reads it on every bench host, and so do the inventory, the survey, the
flashers and the tools. `site.example/registry.yaml` is the smallest registry that works;
`tests/fixtures/registry.yaml` exercises the rest.

This page is the **contract**: the keys the tool reads, what each means, which are required.
A site may carry any other key; the tool ignores it. A unit test
(`tests/unit/test_registry_contract.py`) fails when the code reads a key this page does not
name, so a site's own key never becomes a requirement by accident. `devreg lint` lists a
registry's keys that the contract does not know.

## Why names, and why devreg

Raw device identifiers are not stable: `/dev/sdX` follows plug order, a USB bus id changes
between plugs, and two boards of one model are indistinguishable by VID:PID. A tool that
hardcodes `/dev/sdc` can flash another session's board, or the workstation's own disk. So every
rxrbench write to hardware names its device and goes through `devreg`: `resolve <name>` to find
it, `guard <name> <path>` immediately before writing. Identity comes from the registry, presence
from what the host's sysfs shows; nothing probes a radio or opens a port to decide.

| Command | What |
|---|---|
| `devreg list` | registered devices and whether each is present now |
| `devreg scan` | present hardware not yet registered, as a paste-ready block |
| `devreg resolve <name>` | the path; rc 1 absent, rc 2 ambiguous, never a guess |
| `devreg guard <name> <path>` | asserts the path is that device; run right before the write |
| `devreg resolve --route <bearer> <name>` | the address a route that is not a path records |
| `devreg guard --route <bearer> <name> <address>` | refuses any other address |

`guard` refuses a path on a `protected` list, one with a mounted filesystem, and one that is
not what the name resolves to right now. All commands and exit codes: `devreg/README.md`.

**Ambiguity is an error, not a coin-flip.** When two devices match one name, devreg refuses
(rc 2) rather than pick, because that is exactly the case where a guess writes the wrong board.
Fix it with a distinguishing key in the registry (`usb_serial`, `serial_by_id`), a `host` pin,
or by unplugging the other board.

To add a device, plug it in alone and run `devreg scan`, or let `just survey` and `just enroll`
write the entry (below). Prefer a serial-bearing match over a bare VID:PID, which names a model,
not a unit.

## The file

| Key | Required | Meaning |
|---|---|---|
| `version` | no | not read; `1` by convention |
| `hosts` | by the inventory | the bench hosts, by the name a layout uses (`hosts.<h>`) |
| `devices` | **yes** | every device, by the name every tool uses (`devices.<name>`) |
| `protected` | no | paths `devreg guard` refuses on every host (a workstation's disks) |

## `hosts.<h>`

| Key | Required | Meaning |
|---|---|---|
| `ssh` | one of `ssh` or `local` | `user@address`: how the controller reaches the host |
| `local` | one of `ssh` or `local` | this host is the machine the registry is read on, when `hostname` does not say so |
| `hostname` | no | the host's real hostname, when it is not the key (devreg finds the local host by hostname first) |
| `port`, `ssh_key`, `known_hosts` | no | the inventory's ssh connection details |
| `protected[]` | no | paths `devreg guard` refuses on this host, in addition to the global list |
| `description`, `bluetooth`, `usbipd` | no | shown by `devreg hosts`, read by nothing else |

The protected lists are unioned, never replaced: `/dev/sda` is one host's system disk and
another's boot drive, and over-protecting only refuses a write.

## `devices.<name>`

| Key | Required | Meaning |
|---|---|---|
| `kind` | no | `serial` (a console, the default assumption), `block` (a disk or card), `net` (reached over the network), `swd` (a chip behind a debug probe), `sim` (a simulated board), `usb` |
| `aliases[]` | no | other names the device answers to; an alias two devices share is refused |
| `match` | no | how the device is told apart on a host (below); nothing matches without it |
| `match_also[]` | no | more `match` blocks: a board's other USB faces (a stock app's native USB beside the chip's ROM face); a hit on any face is the board |
| `firmware` | no | `stock` groups the board as running a vendor's firmware; default `unknown` |
| `destructive` | for `kind: block` | the layout may write the whole device (`card_write`) |
| `bootloader` | no | the bootloader's name, read by the boot play when the survey has none |
| `reach` | no | the routes to the device, by bearer (below); none means one live USB route that can do everything |
| `policy` | no | per-device rules (below) |
| `sim_exe`, `sim_seed`, `sim_console`, `match.sim_device_id` | for `kind: sim` | the simulated board's program, seed and console (`pty` by default) |

### `match` (and each `match_also[]` block)

| Key | Meaning |
|---|---|
| `usb_vid`, `usb_pid` | the USB ids, lowercase hex; alone they name a model, not a unit |
| `usb_serial` | the USB serial string: a chip id (16 hex) or a MAC (12 hex or with colons) that survives every firmware and bootloader mode |
| `serial_by_id` | the `/dev/serial/by-id/` name (a USB-JTAG face carrying a MAC, or a bridge given a serial of its own) |
| `block_by_id` | the `/dev/disk/by-id/` name of a block device |
| `host` | pins the match to one host: two identical bridges (factory serial `0001`) on two hosts |
| `sibling_vid`, `sibling_pid`, `sibling_serial` | a bridge with no serial counts as this device only beside a device with these ids on the same hub (a board's own second chip) |

Comparisons are case-insensitive; a block with none of these keys matches nothing. A device
that one entry names by a distinguishing key (`usb_serial`, `serial_by_id`, `block_by_id`,
`sibling_*`) is that entry's, and an entry by `usb_vid`/`usb_pid` alone does not count it. Two
entries both by make alone stay ambiguous to each other.

### `reach.<bearer>`

Bearers: `usb`, `ssh`, `swd`, `ble`, `wifi`, `eth` (wired Ethernet), `lora`. For each verb
(`shell`, `flash`, `config`, `diag`) the route is chosen in that bearer order among the live
routes that can do it; a layout may pin one. `devreg reach <name>` prints them.

A plugin may bring a bearer of its own: a route that carries an image to the device itself
([PLUGINS.md](PLUGINS.md#a-route-that-delivers)). Its entry has the same keys (`host`, `addr`
for the device's address on that route, `state`, `can`), and it sorts after the bearers above,
so it is chosen for a verb only when pinned or when no other live route can do it.

| Key | Meaning |
|---|---|
| `host` or `hosts[]` | the bench host(s) the route is on |
| `state` | `live`, `dormant`, `unproven` (the default for a non-USB route; USB defaults to `live`) |
| `can[]` | the verbs the route can do; a USB route with none can do all (marked assumed) |
| `addr` or `ip` | the address of a route that is not a path (`ble`, `ssh`, `wifi`, `eth`): what `resolve --route` prints and `guard --route` checks; one of the two (both only if equal) |
| `port`, `user`, `jump`, `identity`, `known_hosts` | ssh details (`user` defaults to `root`, `port` to 22); on `eth` or `wifi`, `port` is MCUmgr's UDP port (default 1337) |
| `probe`, `chip`, `uid`, `nrst` | a debug probe route: the probe's name, the chip, its unique id, whether the probe drives reset |
| `node` | a mesh node id, for a `lora` route |
| `tool` | the program that speaks this route, where one is named |
| `modes.<m>` | per firmware mode of a USB route: `by_id` (the port's by-id name), `usb` (`vid:pid` in that mode), `can[]` (unioned into the route's), `enter` (how to get into that mode) |
| `confirmed`, `note`, `modes.<m>.note` | shown by `devreg reach`, read by nothing else |

### `policy`

| Key | Meaning |
|---|---|
| `board_tag` | the board type: must be in a `catalog/boards.yml` (core's, a plugin's or the site's) and among a declared image's `board_tags`; gives the chip and PSRAM a flasher needs |
| `chip_mac` | an ESP32's factory MAC (eFuse) as its ROM loader reads it. Every esptool write (flash, bootloader, erase, region and chip backup and restore) reads the chip's MAC and refuses another, and `esp-image` checks it in its own loader session before writing. Needed where `match` does not name the chip (a USB-UART bridge with no serial, such as a CH340); a `usb_serial` that is the MAC, or a `serial_by_id` that carries it, serves without it |
| `compatible` | the compatible string an A/B update bundle must carry for this Linux device |
| `may_transmit` | `false` keeps the RF check from letting the board transmit |
| anything else | printed by `devreg policy <name> [<key>]` for a site's own tools |

## Where the registry is read from

In order: `DEVREG_REGISTRY`; a `registry.yaml` beside the devreg script (the self-contained copy
`just hosts-setup` and `devreg deploy` put on a bench host, which wins over everything but the
environment, so a bench host never reads a stale site file); then the rxrbench site the script
belongs to, when it runs from the repository. The checkout is the source; a host's copy is
derived and pushed, never edited there.

## What the tool writes

`just survey` writes nothing to the registry; its facts go to `<site>/facts/`. Ids it reads off a
USB serial by the device kind's rule carry `ids_from: usb serial` until the firmware's own
identity confirms them.

`just enroll` writes a new entry: `description`, `kind`, `destructive`, `owner`, `aliases`,
`hardware.{soc,psram,flash}`, `identity`, `match` (`usb_serial` or `serial_by_id`) and
`reach.usb.host` (`owner`, `hardware` and `identity` are for the site, not read by the tool).
It writes `policy.board_tag` when the survey's facts fit exactly one board type in the merged
catalogs: by the Zephyr board a firmware reported (a type's `zephyr_boards`), by a bootloader's
USB ids (`uf2.bootloader_usb`), by the label of a built-in debug probe's drive (`probe_drive`),
or by the USB device kind the survey named.

A board behind its built-in debug probe (a Nucleo's ST-Link) is enrolled as two entries, from
either the probe's USB serial or the chip's unique id:

- the probe: `kind: usb`, matched by its `usb_vid`, `usb_pid` and `usb_serial`, named
  `--probe-name` (default `stlink-<board>`);
- the board: `kind: swd`, `policy.board_tag` from the probe's drive, `reach.swd` with the probe,
  chip and unique id; when the probe has a serial port, also a `match.serial_by_id` and a `usb`
  route for `shell` and `diag` (the board's console).

It refuses, writing nothing, when the survey did not read the chip, the probe has no serial of
its own, no single board type claims the drive, or the chip's id is already registered.
