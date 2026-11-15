# rxrbench.launcher

ESP32 devices that run [Launcher](https://github.com/bmorcelli/Launcher), an app manager that
sits in its own partition and installs other firmware images beside it, one OTA slot each. The
plugin brings the `launcher` family: a board is declared with the Launcher image for its board
type and the apps its slots should hold, and `just plan` / `just apply` bring it there through
Launcher's **serial console**. Launcher's **WebUI** is a second route, for a board with a
registered wifi address. The plugin contract it fills in:
[docs/PLUGINS.md](../../../../docs/PLUGINS.md).

## How such a device works

- **Each installed image gets its own OTA slot**, sized to the image and labelled from its
  name (six lowercase letters and digits: `buddy` -> `buddy0`, `cam-stream` -> `camstr`).
- **A power-on reset boots Launcher**; any other reset (a software restart, a panic, a
  watchdog) follows the otadata. Launcher shows a boot window, then boots the selected app
  unless a key (or `nav SelPress` on the console) enters the menu.
- **The console and the WebUI exist only while Launcher runs.** Once an app is booted, the
  device is that app.
- **Launcher updates itself** from its `.bin`, installed like any app. Its merged image is
  never written at 0x0 on a chip that has it: its table holds only Launcher's own partitions
  and every installed app would be orphaned. Core's ESP32 flashers read the partition table and
  refuse exactly that (`bench_app_manager_overwrite`).
- **An installed app cannot be read back** through Launcher. An app is known by its slot's
  label and size and by the install record this tool keeps in the board's facts (slot, image,
  sha256); a slot with no record is written again, and the plan says why.

## Declaring a board

```yaml
firmware: {family: launcher, image: launcher-cyd-2-usb-2.9.1}   # Launcher itself, by board type
launcher:
  apps:
    camstr: {image: cam-stream}       # slot label -> a catalog image of kind `app`
    wifite: {image: wifi-test}
  boot: camstr                        # the slot the otadata selects
  boot_to_app: true                   # Launcher settings this tool keeps (also boot_timer,
  wui_user: admin                     # dev_mode, auto_connect)
  wifi: [{ssid: lab-iot}]             # saved networks; each password a declared secret
  prune: false                        # slots not declared are left alone (true deletes them)
```

Catalog images of this family carry `kind: manager` (a release's merged file for one board
environment) or `kind: app`, with `flasher: launcher`. The secrets live in the layout's SOPS
group_vars as `launcher_secrets.<board>.wui_password` and
`launcher_secrets.<board>.wifi_psk.<ssid>`; `just secrets` compares them without showing them.

A board with no Launcher yet gets the merged image at 0x0 (`launcher_install`, after an erase)
when its layout says `bench_first_flash: true`, with the same checks as any first flash.

The plan names what the layout does not own (`bench_foreign`): an app it does not declare is
reported as other firmware, after a line naming the chip (`Launcher in test at 0x10000 with
apps: camstr, wifite`).

## The serial console

115200 baud, no echo, newline-ended. The family uses `version`, `partitions` (the table, boot
slot marked), `settings get|set`, `wifi add|del|list`, `flash firmware <name> <size>` (an app
from raw bytes, 2 KB pieces each acknowledged), `partition delete`, `reboot` and
`nav <Key>Press`. The broker speaks it as the `launcher` dialect (`host/dialects.d/launcher.json`),
keeping passwords out of its log. To reach Launcher from a running app the module pulses the
port's modem lines (a power-on through a UART bridge's auto-reset) and answers the boot window,
with a key press or, where the board type says `launcher_enter: settings`, by switching
`bootToApp` off for the window and back on after.

Every write is read back: a setting through `settings get`, an install as its slot in the
table, Launcher's update as the version it then answers.

## The whole chip

The plugin declares its flasher `chip`-capable, so `just backup`, `just restore-chip`,
`just snapshot` / `just switch` and `just erase-chip` take a Launcher board as any ESP32: every
byte, Launcher, its apps and their data partitions, through esptool after a reset. There is no
region level, so `just erase`, which erases one config region, does not apply.

## The WebUI route

`just launcher --board <name> ...` (`tools/launcherctl.py`) resolves the board's wifi route
through devreg and guards it before a write; `--url http://host` reads a WebUI by address and is
refused for a write. The password comes only from an owner-only file
(`$XDG_CONFIG_HOME/rxrbench/launcher.auth` by default), never an argument or an environment
variable; the session token is kept per host under `$XDG_CACHE_HOME/rxrbench/launcher/`. Change
Launcher's default password before the device is on a shared network.

## Parts

| | |
|---|---|
| `plugins/plugin_utils/bench.py`, `families/launcher.py` | the family row and what it knows (secrets, layout checks, survey tags) |
| `plugins/module_utils/` | `console.py` (parsers, enter, read, leave), `plan.py` (declared state against the board: the steps, in order), `images.py`, `webui.py` |
| `plugins/modules/` | `launcher_state` (read-only), `launcher_apply` (plan and apply), `launcher_install` (Launcher onto a chip that has none), `launcher_images` (the declared images as files), `launcher_facts` (WebUI, read-only) |
| `host/bin/launcher_serial.py` | installs an app over the console's raw-byte protocol, on a bench host |
| `catalog/boards.yml` | the board types (chip, PSRAM, `modem_rest`, `launcher_enter`) |

## On hardware

Applies have completed on three boards: Launcher's own update to 3.0.0 on a CYD 2432S028, a
ThinkNode M9 and a LilyGo T-Embed CC1101 Plus (2026-11-14), and on the T-Embed a round trip
3.0.0 → 2.9.1 → 3.0.0 with its apps reinstalled (2026-11-15). Both boards on 2.9.1 were first
read whole through their brokers (`tests/fixtures/launcher/` holds what each printed), and the
CYD's whole chip backed up through esptool and restored over another firmware. The modules are
proven against a fake console behind a real broker (`tests/unit/test_launcher_*`);
`tests/hw/test_launcher_boards.py` reads real boards, read-only. Board quirks (modem-line rest
states, native-USB reset, settings carried between boards):
[docs/HARDWARE.md](../../../../docs/HARDWARE.md).

## Known limits

- The console has no command to select the boot slot: an install selects what it wrote, so the
  declared boot slot is installed last; otherwise the difference is reported as needing the
  WebUI.
- A serial install carries the app image only, not a data partition the app needs.
- Entering Launcher by reset needs a power-on the bootloader can see: a native-USB chip's USB
  reset is not one, and a key-boot build needs its key held. On such a board (the T-Embed) each
  step that reboots, an install or Launcher's own update, is the last of its apply: what it did
  is recorded (Launcher's own update proven by reading its partition back through esptool, which
  needs the board's `bench_mcu`), and the apply fails naming the steps left, for the next apply
  once someone has put the board in Launcher.

## Recipes

What this plugin adds to the tool's `just` (its `launcher.just`, which the justfile imports).

<!-- commands: generated by `just docs-readme` from the recipes' [group] and [doc]; edit those -->
**Bench hosts and devices**

| | |
|---|---|
| `just launcher [args ...]` | an ESP32's Launcher WebUI (`--board <name> status`, `install x.bin`, ...); `--board` resolves the registry's wifi route through devreg, `--url` reads only |
<!-- commands: end -->
