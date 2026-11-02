# Hardware

What has run on real boards with this tree and what has not, then the hardware behaviour a bench
will meet, each as a rule and the reason for it. The code states each rule where it applies.

## Proven on hardware, and not yet

"Proven" means run on a real board with this tree, the device's own answer checked afterwards.
"Sim" means proven only on a simulated bench ([`SIMULATED.md`](SIMULATED.md)).

### Core: identity, flashing, backups

| Capability | Proven on hardware | Not yet |
|---|---|---|
| Survey, enroll, the serial brokers, devreg's guard | yes | a mesh companion device (a binary protocol on USB) in the survey: tested against a fake only |
| First flashes and family switches by `apply` | ESP32-S3, nRF52840, RP2040, RP2350 | |
| esptool, UF2, picoboot writes | ESP32-S3; nRF52840 (UF2); RP2040 and RP2350 (picoboot) | |
| Asking a running app for UF2 mode in its family's way; `bench_uf2_enter_as` | | unit tests only (UF2 updates themselves are proven) |
| Whole-chip backup, restore, snapshot, switch | ESP32-S3, RP2040; an ESP32 under Launcher (backed up and put back) | |
| Whole-chip erase | RP2040 (Pro Micro, the board found again afterwards) | |
| Config-region backup and restore | ESP32-S3 mesh nodes; RP2040 and RP2350 (`zapp`) | nRF52840: none, by design |
| MCUboot by `just boot` | yes | |
| nRF52840 UF2 bootloader update (`just boot`) | read only (the version recorded) | the write |
| MCUmgr over BLE | an update down a build and back, config and key kept | the first bond: made by hand, once |
| MCUmgr over UDP (Ethernet) | an STM32F767 under MCUboot, an update both ways by `apply` | |
| MCUmgr over serial | | sim and QEMU only |
| SWD through a debug probe | identify, read, guard, plan, write, read-back, whole-chip and region backup, restore and erase: an STM32F103 on an ST-Link V2 | a plugin's own probe flasher: against a file-simulated chip only |
| Images built elsewhere (HEX or ELF staged with an address) | | against a file-simulated chip only |
| Card (SD) writes | | code and unit tests only |
| Spot check depth (a plan taking an ESP32-S3's last full read while the plug watcher saw no change) | yes | |
| `just locate` | | the modules report the LED; nobody has watched one |

### By plugin

| Plugin and family | Proven on hardware | Not yet |
|---|---|---|
| mesh `zephyrtastic` | images by esptool, UF2 and MCUmgr over BLE; radio, channels and its config; the identity key escrowed, a factory reset, then `apply` bringing the node back as itself; admin keys; device tests and a scenario on air | |
| mesh `zephcore` | the image by `apply` on an ESP32-S3; config and admin password; key escrow, an erase, the key given back | MCUmgr over BLE |
| mesh `meshtastic` | 2.7 and 2.8 releases; config over USB and over the network API; key escrow after a factory reset; `apply` switching a board to it and back | its WiFi is set by hand: no declaration for it |
| mesh `meshcore` | identity by `ver`; config; key escrow (a key swap, a whole erase); a first flash refused where its boot radio breaks the RF rules; a scenario on air | the admin password is written, never compared (the firmware prints no fingerprint); a companion build |
| linux `linux_slotguard` | a Raspberry Pi 400: a signed bundle into the other slot, the health gate after its soak, a rollback, the host key escrowed | |
| linux `linux_rauc`, `linux` | | sim only; `just card` has written no card |
| zephyr `zephyr_settings` | `zapp` on RP2040 and RP2350: settings and secrets, the device key escrowed, lost and given back, kept across an update; settings by digest | |
| nuttx `nuttx_nsh` | an image written to an RP2040 and read back by `picoboot` | configuration over NSH: sim only, and not offered on an RP2040 (below) |
| launcher `launcher` | reads of a LilyGo T-Embed and a CYD 2432S028 on Launcher 2.9.1 | an install: the one apply tried failed when the board restarted partway through |

## Serial ports

- **Every reader goes through the board's broker.** pyserial takes no lock, so two processes
  reading one port each get part of the bytes and neither errors. That looks like a flaky board.
  The device lock (`devreg/devlock.py`) also scans `/proc` for holders that never took it.
- **Never open a port to see whether a board is alive.** Opening a native-USB CDC port (a Heltec
  V4's) resets the board.
- **Writes time out.** A crashed or erased board stops reading and the USB buffer fills; a write
  without a timeout waits for ever. The broker closes the port after two seconds and retries on
  the next open.
- **Commands are typed in chunks.** A Zephyr shell's receive ring can be 64 bytes and drops a
  longer burst silently; the broker sends each chunk after the last was echoed.
- **The prompt is not the end of a command.** A log line arriving mid-command redraws the prompt.
  The broker sends a marker line after the command and reads to the shell's complaint about it.
- **A busy console is not a dead board.** For a minute after a reboot a node can print thousands
  of log lines and mangle a command's echo; a read after a flash waits out the settle time.

## USB identity and hubs

- **Pin bridges that share a serial.** Every factory CP2102 reports serial `0001`. A registry
  entry pins such a board to its host (`match.host`), and `just cp210x-serial` gives a bridge a
  serial of its own.
- **Match a CH340 beside a chip that has a serial.** A CH340 has none (every generic one is
  `1a86:7523`). On a board with a serial-bearing chip behind the same on-board hub, the bridge is
  matched beside it (`match.sibling_*`). Two CH340s also share one `/dev/serial/by-id` name that
  follows the last one plugged in, so a port with no serial is locked by its `by-path` name.
- **Protected disks are listed per host.** A block-device name means a different disk on each
  host (a Pi booting from USB has its root on `/dev/sda2`).
- **Never trust a tty number.** A board that restarts while another is unplugged can come back
  as another `ttyACM`. Every open resolves the board by name and guards the path, and the lock is
  keyed by the by-id name, so one board's old and new tty are one lock.
- **Replug a port that failed enumeration** (`unable to enumerate`); it stays failed.
- **Never cycle a hub's power to reset a board.** A ganged hub can also carry the host's own
  root disk.
- **A 1200-baud touch over usbip may not land.** It is retried, not taken for a dead board.

## ESP32-S3

- **Two USB faces.** USB-Serial-JTAG (`303a:1001`) is the same in the app and in ROM download
  mode, its serial the base MAC. An app on native USB (`303a:0002`, TinyUSB, as a Vision Master
  E290's factory app) is another device, its serial the MAC without colons. esptool's first
  connection resets it into ROM mode, where it reappears as `303a:1001` under another name; the
  tool puts such a board into ROM mode first and guards the ROM port in its turn.
- **espefuse before esptool.** The last tool's hard reset returns the chip to its app; with
  espefuse last, the board stays silent in ROM mode until reset by hand.
- **A first flash erases the chip, and is refused unless it is safe.** Leftover flash can break
  the config file system's mount. The erase also removes the bootloader, so MCUboot and the app
  are written together. It needs the survey to show secure boot and flash encryption off, ROM
  download on, and the PSRAM of the registered board type.
- **An app manager owns the partition table.** A manager in the `test` partition at 0x10000 that
  installs images in OTA slots: writing its app slot or a merged image at 0x0 orphans every
  installed app. Every ESP32 writer reads the table first and refuses without
  `bench_app_manager_overwrite`.
- **MCUboot is compared by its signing key.** A bootloader built with another key boots no image
  signed with this one; an older build with the same key boots today's images. Bytes are compared
  only on request.

Under GDB on its USB-JTAG, OpenOCD shares the console's cable (pinned to the board's serial);
use `hbreak` (code runs from flash), end with `monitor resume` before `detach`, and stop OpenOCD
before flashing.

## nRF52840 with a UF2 bootloader (XIAO, Heltec T114)

- **One identity in every mode.** The USB serial is the chip's FICR device id in the app, the
  bootloader and the factory app.
- **A 1200-baud touch does not give the drive.** It sets GPREGRET 0x4E, serial-only DFU. The
  drive needs 0x57, which only the running app can request (or a double tap of reset). A board in
  serial-only DFU needs a replug.
- **The bootloader is matched by USB ids, never by name.** A XIAO radio kit's factory app calls
  itself "...-BOOT" and is not a bootloader; a T114's app and bootloader share one product
  string.
- **A bootloader update is staged in the app area.** It writes near the top of the app slot
  first, so an installed image that reaches that far is damaged and the board goes silent until
  a double tap. A small carrier image goes on first.
- **The first serial-DFU session fails; the second succeeds.** adafruit-nrfutil exits 0 on that
  failure, so success is read from "Device programmed." and the bootloader leaving the bus.
- **The drive can take 30 s to appear** (the bootloader leaves the first SCSI read unanswered
  until its timeout). An automounter's mount is reused once matched by serial.
- **Without a valid app, an OTAFIX bootloader stays in BLE DFU** and does not show on USB. The
  mesh plugin's recovery tool flashes it over legacy BLE DFU (about 3.4 KB/s).
- **The drive is checked before it is written**: the USB serial behind the mount and the drive's
  own files must be the named board.
- **Serial DFU wipes Bluetooth bonds; a UF2 copy of the app slot does not.**
- **No region or chip backup.** The drive never holds the region the config lives in.

## Bluetooth LE and MCUmgr

- **One adapter, one user.** Every session takes a lock on `ble:<adapter>`; a tool reaching the
  host over ssh wraps its command in `devlock.py run ble:hci0 -- <cmd>`.
- **Timeouts run on the host.** Killing a local ssh client leaves the remote command running,
  and an orphaned `bluetoothctl` holds the adapter for every later session.
- **Make the first bond by hand.** A board whose MCUmgr service wants an encrypted link times out
  a host with no bond. Pair once (`bluetoothctl`: `agent NoInputNoOutput`, `default-agent`,
  `pair <addr>`); the flasher uses the bond and repairs it when it goes stale.
- **A node advertises only with a free connection slot.** The flasher drops a link the host holds
  before it scans.
- **A bond is removed only on the board's word.** A stale bond and a dropped link look the same
  from the host, and removing a good bond leaves the board refusing to pair again. The board is
  asked which bonds it holds; a plan removes none.
- **Wait for both MCUmgr and the console** after a BLE update; MCUmgr answers seconds earlier.
- **Small frames over a shell transport.** The serial transport uses a line length of 128 and 10
  line buffers; a larger first chunk is never answered. `smpmgr shell` fails with EMSGSIZE above
  the device's dummy shell buffer (2048 bytes); structured SMP requests have no such limit.
- **A slot is identified by its SHA-256 TLV**, which MCUmgr reports. A version alone proves
  nothing. MCUmgr's `0.4.46.45` is MCUboot's `0.4.46+45`; the tool reads both as the latter.
- **Swap-using-offset reports the secondary slot at the upload's place.** In this mode (Zephyr
  4.5 sysbuild's default on an STM32F767) an upload lands one sector into the secondary slot, and
  after a confirmed update that slot reports the image just uploaded, not the previous one. The
  tool decides from the active slot only. A test boot that is never confirmed is reverted on the
  next reset.
- **Rebuilds are new images.** The same version rebuilt has another hash; keep the signed file a
  board runs if it is to plan clean later.

## A chip behind a debug probe

- **The probe says nothing about the chip on it.** The chip's unique id is read in the same
  session as the work, and the session ends if it differs.
- **Connect under reset** when the reset line is wired: a chip sleeping with its debug clock off
  (an STM32F1 in WFI) cannot be attached while it runs.

## RP2040 and RP2350

- **Found by the serial its firmware reports**: an RP2040's flash chip id (it has no id of its
  own), an RP2350's chip id from OTP. Zephyr reports the RP2350's only with
  `CONFIG_HWINFO_RPI_PICO_CHIP_ID=y`; NuttX reports neither without a board patch.
- **The RP2040 ROM bootloader hides the id.** Its drive has another serial, which is never
  written. The plug watcher asks picotool, read-only, for the registry's candidate ids, so an
  erased board is still seen as itself; picotool selects it by flash id (`--ser`). These boards
  need `picotool` on the bench host (Debian 13 packages it; Ubuntu 24.04 does not) and the board
  types from `site.example/catalog`.
- **Ways into the bootloader differ.** CircuitPython takes a 1200-baud touch; Zephyr needs a shell
  command calling the ROM's `reset_usb_boot`; NuttX has `reboot 3` on the RP2350 and nothing on
  the RP2040 without a patch. A firmware that hangs before USB needs BOOTSEL held while
  replugging (`manual` in the board's ways in).
- **Whole-chip work is slow.** A 16 MB RP2040 reads at about 40 KB/s: minutes for a read, ten or
  more for an erase with its read-back.
- **Long Zephyr settings on an RP2350 can be stored wrong.** With Zephyr 4.5, a value over about
  300 bytes is sometimes stored with bytes lost (256 bytes and under never failed). With
  `zephyr.digest_command` declared, the digest check catches it and `apply` fails ("written, but
  reads back as something else") rather than passing a wrong value.
- **No NuttX config files on an RP2040.** Its flash file system is linked into the image and
  moves when the image grows, so files do not survive an update.

## Simulated hosts and NuttX's sim

- **`/dev/kvm` can exist and not work** (WSL2 without nested virtualization). The tool opens it
  and asks its API version; without KVM the simulated host runs under TCG, too slowly for its
  setup.
- **The sim's NuttX builds turn DMA console mode off** (it holds every 256th byte) and raise the
  line length to 512 (NSH drops the end of a longer line). The sim has no reset, only `poweroff`;
  the bench starts it again.

## Linux with RAUC

- **Nothing of the tool runs on the device.** An image may have BusyBox and no Python; it is
  driven over ssh with `rauc` and `systemctl` only.
- **A slot is good when its health unit says so**, not when the install returned. The wait
  covers the reboot and the unit, and a rollback is reported as one.
