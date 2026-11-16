# Changelog

## Unreleased

### New

- **Zephyr boards whose application has them: `just locate` and the state line**: a layout says
  `bench_locate: shell` (and `zephyr.locate_command`, zapp's `zapp locate` by default) to blink a
  board's LED, and `zephyr.state_command` to have `just facts` read the board's state line,
  checked by core's parser, its version held against the declared image's.
- **A WiFi key as a Zephyr secret**: the zephyr plugin's secret kind `wifi_psk` (a network key the
  device joins with), declared and compared by digest as `psk` is, kept out of every log.
- **zapp 3.x for real boards**: the zephyr plugin's example application chooses its features by
  Kconfig (digest, reboot, bootsel, state line, locate, self-tests, WiFi from settings) and lists
  them; `just zapp-build <board> <version>` builds it for a Pico 2 W, Pico W, KB2040 or Pro Micro
  RP2040 and stages it with its features (`tools/stage_raw.py --features`).
- **Boards a CI job may exercise**: a layout marks a board `bench_ci: {run: true}` (and
  `destructive: true` for its erases and family switch); `tools/ci_boards.py <layout>` lists the
  boards so marked, and `tools/exercise.py <layout> <board> confirm=apply --ci` refuses any other
  and takes the destructive verbs from the board, never from the command line.
  A marked board that is not plugged in to the host is reported as not present (exit code 3),
  not run and not recorded, so unplugging a board does not read as a failure.

- **A CI image**: `ghcr.io/roperscrossroads/rxrbench-ci`, a Wolfi base assembled by apko with
  the tool's pinned Python environment on it (`ci/image/`). The unit job runs in it when one was
  built from the same requirements, and installs them itself otherwise.

- **Images say what they provide**: a catalog entry may list `features`; the board exercise's
  second image for `update-there-and-back` must provide all the board's image lists, and the
  verb says which features no other image has when none does.

### Fixed

- **A board unplugged part way through the board exercise read as a failed board, and a play
  could run without end**: a board reached by USB that leaves the bus (and stays away past a 20 s
  grace, so a reboot is not mistaken for it) now ends the run as interrupted: the step it left
  during is `interrupted`, the rest are not run, the final plan is skipped, nothing of it goes to
  the ledger, and the exit code is 4 (DISCONNECTED). Each play is stopped at 30 minutes (its whole
  process group) and its step fails as timed out.
- **A board registered by its RP2040 bootloader serial was never seen as plugged in**: twelve hex
  digits were read as a MAC, but the relay files such a serial as VID:PID:serial; a match that
  gives the VID and PID is now known both ways.
- **picoboot gave up on a board still on its way back from the bootloader**: asking its
  application for the bootloader again (a backup's chip read after its region read) looked the
  board up once; one that starts slower (a Pico 2 W running zapp 3.0, about 4 s) was absent, and
  every way in failed. An absent board is now waited for (15 s).
- **The exercise could not read a layout with SOPS vars outside `just`**: its own inventory reads
  ran without the pinned tools on PATH (its plays had them), so a CI job, which calls it
  directly, failed on such a layout before it ran anything.
- **The exercise found no region backup on a real board**: a region backup is named for its time
  and the build it was read from; the exercise looked for the time alone, failed backup-regions
  and skipped every write. It now takes the whole name.

## 0.2.0 (2026-11-15), alpha

### New

- **The board exercise**: `just exercise <layout> <board> confirm=apply` takes one board through every verb of the
  tool that applies to it and records what happened (`runs/<time>/exercise/<board>.json`). What
  applies is derived from what the inventory publishes for the board (the flasher's capabilities,
  the family's spec, its routes, secrets, tests and go-ahead) and from the images catalogued or
  staged for its board type, never from a list per board; without `confirm=apply` it says what
  would run, with a reason per verb, and runs nothing. The reads run first, then the writes undone
  within the run (each after the backup that undoes it: a reboot, a no-op apply, the region backup
  written back, a snapshot and the switch back, an update to a second image and back, the family
  switch and back, the key restore planned), then only with `--destructive` the destructive ones
  each followed by its restore (a config region erased then apply and keys-restore; the chip
  erased then restore-chip). Every restore writes back the backup this run took; an update or a
  switch that fails comes back by apply, or by restore-chip from this run's backup. The board is
  planned at the start and the end and the record says whether it came back as it was; a start
  that is not clean or a failed write skips the writes after it, and a board its layout does not
  let the tool write gets the reads alone. The same exercise runs as `tests/hw/test_exercise.py`
  (one test per verb, boards named in `RXRBENCH_HW_BOARDS`) and on the simulated bench.
- **Re-test only what changed**: each exercise step records the features it exercises on the
  board and a fingerprint of their code and the board's own inputs; `--record` appends every step
  to the site's ledger (`exercise/ledger.jsonl`), with the board type and firmware.
  `--stale-only` leaves out a verb that passed last time against the same code, board type and
  firmware (`--max-age DAYS` makes an old pass stale; a firmware with no identity is never fresh);
  `--only` runs the verbs named.
- **The capability matrix from the records**: `just matrix` writes the site's
  `CAPABILITY-MATRIX.md` table from the newest exercise record per board (verbs by boards, what
  each record says and no more), and `--ledger` a table by board type and firmware; `--public`
  writes one with no board, image or build names, past the privacy check.
- **The feature index**: each collection keeps `features.yml`: its verbs, families, flashers,
  routes and capabilities, the code each depends on and the tests that prove it.
  `just features` checks it against the code (a family, verb, flasher or route it lacks, a missing
  path or test, a feature with neither a test nor a stated gap); `--gaps` lists what is unproven
  on hardware.
- **Launcher 3.0.0**: the launcher plugin enters and drives Launcher 3.0.0 as well as 2.9.1:
  the boot window is answered until it closes, a CYD's Launcher is entered through its settings
  (3.0.0 drops the serial key press), and its flags are sent as JSON booleans.

### Fixed

- **Launcher's flags were ignored**: Launcher skips a flag sent as 0 or 1 and still answers
  "settings loaded" (every release from 2.9.1 to 3.0.0); the settings entry, its restore and the
  plan now send `false` and `true`.
- **RP2040 and RP2350 flash reads were slow, and a failed read could leave a partial backup**:
  reads through the ROM bootloader now go in 128 KB pieces (about four times faster over a whole
  8 MB chip), each piece checked, and a read is renamed into place only when whole.
- **snapshot and switch failed on an SWD board**: they now hand the chip module the board's
  flash route, as backup, restore and erase do.
- **Launcher's own update could be left half-written**: after Launcher installs its own update
  the board copies it over Launcher's partition; the apply took the port back and reset the board
  at once. The port is now held until Launcher says its partition table is updated, and an update
  not seen within `self_update_wait` fails with the board left alone, not reset.
- **Whole-chip reads of a large ESP32 timed out behind a UART bridge**: the ROM loader's 115200
  baud read a nearly full 16 MB chip in longer than the session allowed. Reads and writes move to
  921600 after the stub loads, unless the port is the chip's own USB (`RXRBENCH_ESP_BAUD`
  overrides it), and a session may take 30 min.
- **An apply that changed Launcher's version reported a failure after the update succeeded**:
  Launcher 3.0.0 reboots into the new copy without saying `OK flashed`; the reboot after the
  whole image was acknowledged now counts as done. On a board that does not come back into
  Launcher after a reset (native USB, such as the T-Embed), the new Launcher's partition is read
  back through esptool and compared with the image instead of asking its console (the board's
  `bench_mcu` names the chip).
- **On a native-USB board an install was done again on every apply**: after an install the
  board boots its app and the apply cannot get back into Launcher; it failed without recording
  the install, so Launcher 2.9.1 (which keeps no record of its slots) was given the same install
  next time. What was done is now recorded, an install as the last step stands, and the apply
  fails naming the steps left for the next one, once someone has put the board in Launcher.

### Known issues

- **A native-USB board takes one rebooting step per apply**: Launcher shows only after a
  power-on, and the bench cannot power-cycle a native-USB board, so each install or Launcher
  update on the T-Embed needs the board put in Launcher by hand (its button held at power-on)
  before the next apply. Boards behind a UART bridge (CH340, CP210x) are not affected.

### Tests and CI

- `just test-changed` runs the tests a change needs, chosen from the feature index; a pull
  request runs that selection and only the simulated benches it touches, a push everything.
- The tests run in parallel, one worker per core (pytest-xdist): the unit tests in about 2.5 min
  on four cores instead of 25, the simulated benches in about 23 min instead of 44.
- The Zephyr and NuttX sim jobs run in a CI image with the Zephyr SDK's Arm toolchain
  (`ghcr.io/roperscrossroads/zephyr-arm`), and the built sim images are cached between runs:
  job setup on GitHub went from about 7 min to 1.

## 0.1.0 (2026-11-02), alpha

- First public version, of alpha quality: the `rxrbench.core` framework and the `mesh`, `linux`,
  `zephyr`, `nuttx` and `launcher` plugin collections. It has rough edges and bugs; verify what
  it does before relying on it (README, "Status: alpha"). Layout keys, registry fields and recipe
  names may change between 0.x versions with no migration.
