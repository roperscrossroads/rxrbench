# rxrbench.zephyr

Generic Zephyr applications as bench devices: any application that keeps its configuration in
Zephyr's settings subsystem and takes the settings shell (`CONFIG_SETTINGS_SHELL`). The plugin
brings one family, `zephyr_settings`, known by the bytes of its image and spoken to through
the Zephyr shell, and an example application, `zapp`, which a simulated bench runs
([docs/SIMULATED.md](../../../../docs/SIMULATED.md)). The plugin contract it fills in:
[docs/PLUGINS.md](../../../../docs/PLUGINS.md).

## A board's declaration

```yaml
firmware: {family: zephyr_settings, image: zapp-1.0}
zephyr:
  device_key: app/key                   # a setting the device makes itself; escrowed
  settings:                             # written through the shell, each when it differs
    app/psk: 9f1c…                      # one word each: the shell splits on whitespace
    tls/cert: MIIB…                     # a certificate as one base64 word
    net/name: zb3
  secrets: {psk: app/psk, cert: tls/cert}   # which settings are secrets, by kind
  facts: [app/version, net/name]        # what `just facts` reads (default: app/version)
  state_command: zapp state             # an app that prints rxrbench's state line: `just facts`
                                        # reads and checks it, its version against the image's
  locate_command: zapp locate           # an app that lights an LED (the default with bench_locate)
bench_locate: shell                     # `just locate` blinks it: `<locate_command> blink <s>`
```

What an application offers beyond the settings shell is the layout's to say, board by board:
the family cannot assume it (most Zephyr applications print no state line and light no LED).
The state line is read in `just facts`, not used as the board's identity: core still knows a
`zephyr_settings` board by the bytes of its image.

The settings step (`roles/board/tasks/settings.yml`, module `zephyr_settings`) reads each
declared setting and writes the ones that differ. Zephyr's settings shell reads back at most
256 bytes of a value (`SETTINGS_MAX_VAL_LEN`), so a longer declared value is refused before
anything is written, and a read the shell reports as truncated is an error. A setting named
under `secrets` or as the `device_key` never reaches the broker's log or a task's output
(`host/dialects.d/zephyr-settings.json` also scrubs a 64-hex key wherever a board prints one).

A firmware that prints a setting's SHA-256 lifts both limits: `zephyr.digest_command` names the
command (zapp's `zapp digest <name>`, answering `digest <name> <length> <sha256>` or
`digest <name> none`). A secret is then compared without being printed, and a value may be as
long as a shell line can write: it is checked by its digest, not read back.

## Secrets

| Kind | Origin | What |
|---|---|---|
| `device_key` | device | The setting `zephyr.device_key` names, made by the device at first boot. `just secrets` compares it with the site's escrow; `just keys-export` escrows it; `just keys-restore` writes it back after an erase. |
| `psk`, `cert`, `wifi_psk`, `token` | declared | The settings `zephyr.secrets` names, compared with the layout's values; the settings step writes them. `wifi_psk` is the key of the network the device joins (zapp's `wifi/psk`). |

## The example application

`sim/zapp/`: at boot it loads its settings, makes a key (`app/key`, 32 random bytes as hex)
when it has none, and writes its version (`app/version`). `just sim-zapp-build` builds three
versions, `zapp-1.0`, `zapp-1.1` and `zapp-2.0`, so a bench has something to upgrade and
downgrade between. On native_sim its flash is the sim board's flash file: settings survive a
reboot and a reflash, and an erase makes a new key. `zapp digest <name>` prints a stored
setting's SHA-256 and length.

`zapp` is an example: its key is only as good as the build's random source. A real
application makes its keys from a hardware RNG.

### On real boards: zapp 3.x and its features

What a build holds is chosen by Kconfig (`sim/zapp/Kconfig`), one file per feature in `src/`, and
the build lists them (`zapp-features.txt`, and `zapp features` on the board), so the image's
catalog entry says what it provides and the board exercise picks a second image that has it all.

| Feature | Command | What it exercises |
|---|---|---|
| `digest` | `zapp digest <name>` | a setting's SHA-256: secrets compared without crossing the console |
| `reboot` | `kernel reboot cold` | `just reboot` |
| `bootsel` | `rp2 bootsel` | the picoboot flasher's way in with no button (`bench_picoboot_enter`) |
| `state` | `zapp state` | rxrbench's [state line](../../../../docs/PLUGINS.md#the-state-line): family, app, version, board target, chip id |
| `locate` | `zapp locate on/off/blink [s]` | the board's LED (a GPIO `led0` or a WS2812 `led-strip`) |
| `selftest` | `zapp test flash/entropy/temp/wifi/all/list` | the board's hardware, one `test <name> pass/fail` line each: a layout binds them as device tests (`bench_tests`) |
| `wifi` | `zapp wifi status/connect` | the CYW43439 on a W board: joins the network the settings `wifi/ssid` and `wifi/psk` name (`secrets: {wifi_psk: wifi/psk}`), DHCP |
| `hawkbit` | `zapp hawkbit status/probe` | Zephyr's hawkBit client: polls the update server the settings `zhb/server`, `zhb/port`, `zhb/id` and `zhb/token` name, takes what it offers and reboots into it (with `--mcuboot --hawkbit`) |

`just zapp-build <board> <version>` builds it for a board of `sim/zapp/hw/boards.yml` (Pico 2 W,
Pico W, KB2040, Pro Micro RP2040: a storage partition at the top of flash, the CDC-ACM console,
the board's features) at 3.0 or 3.1, two versions with the same features so an update has a
pair, stages it with its flash map and features, and prints its catalog entry. It needs a west
workspace with Zephyr 4.5 (`RXRBENCH_ZEPHYR_WORKSPACE`), and each module it lacks given with
`--module DIR` (`hal_rpi_pico`, and `hal_infineon` with its blobs for WiFi).

`--mcuboot` (the Pico 2 W) builds it under MCUboot with sysbuild: MCUboot signed with ECDSA P-256
(`--key PEM`; without it MCUboot's development key, public, for the example only) and zapp signed
for it, with MCUmgr over the shell's serial port and over UDP on WiFi (features `mcuboot`,
`smp-serial`, `smp-udp`), its version the full x.y.0 MCUboot reports. It is staged three ways:
`zapp-<v>-<board>-mcuboot` (MCUboot and the signed app in one UF2, family `bare`, the first flash
through picoboot), `zapp-<v>-<board>-signed` (family `zephyr_settings`, flasher `smp`: an update
over MCUmgr, the board's settings and secrets still the family's) and `mcuboot-<board>` (the
bootloader, for `just boot`).

`--hawkbit` (with `--mcuboot`, on a board with a network) adds Zephyr's hawkBit client on a
thread of zapp's own (feature `hawkbit`): the device polls an update server, takes what it
offers into its second slot and reboots into it. Which server, under which name and with
which token are settings, declared like any other and read before each poll:

```yaml
zephyr:
  digest_command: zapp digest
  settings:
    zhb/server: 192.0.2.10
    zhb/port: "8080"
    zhb/id: node-a
    zhb/token: "{{ bench_hawkbit_token }}"
  secrets: {token: zhb/token}
```

`zapp hawkbit status` prints one line and `zapp hawkbit probe` asks now. An image on its test
boot confirms itself after a minute of uptime, and the client asks the server nothing before
that. Version 3.4 is an image made to fail, for a bench to show an update that does not hold:
it faults a few seconds into its test boot (`CONFIG_ZAPP_HAWKBIT_FAULT_SEC`), the device
resets, and MCUboot returns to the image before it. A board built this way can take its updates through the
[`rxrbench.hawkbit`](../hawkbit/README.md) route (`routes: {flash: hawkbit, read: wifi}`).

## On hardware

`zapp` on an RP2040 and an RP2350 board, flashed by core's `picoboot` route: settings and
secrets through the shell, the device key escrowed, lost and given back and kept across an
update, `just facts`, a region backup and restore, a reboot, and settings by digest. On the
RP2350 a value over about 300 bytes is sometimes stored with bytes lost; the digest check
catches it and `apply` fails. Details: [docs/HARDWARE.md](../../../../docs/HARDWARE.md).

## Recipes

What this plugin adds to the tool's `just` (its `zephyr.just`, which the justfile imports).

<!-- commands: generated by `just docs-readme` from the recipes' [group] and [doc]; edit those -->
**Plan and apply**

| | |
|---|---|
| `just zapp-build <board> <version> [args ...]` | the `rxrbench.zephyr` example application for a real board (`sim/zapp/hw/boards.yml`) at a version, staged with its features; prints its catalog entry |

**Simulated benches**

| | |
|---|---|
| `just sim-zapp-build [args ...]` | the `rxrbench.zephyr` example application at three versions (`zapp-1.0`, `zapp-1.1`, `zapp-2.0`), from any west workspace |
| `just sim-zapp-qemu-build [args ...]` | the `rxrbench.zephyr` example application with a hawkBit client for the emulated MCU with a network (`zqh`, `zqh-b`, and `zqh-bad`, made to fail): the simulated device of the hawkBit route (Zephyr SDK) |
<!-- commands: end -->
