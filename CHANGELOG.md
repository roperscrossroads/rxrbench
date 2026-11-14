# Changelog

## Unreleased

- **The board exercise**: `just exercise <layout> <board> confirm=apply` takes one board through every verb of the
  tool that applies to it and records what happened (`runs/<time>/exercise/<board>.json`). What
  applies is derived from what the inventory publishes for the board (the flasher's capabilities,
  the family's spec, its routes, secrets, tests and go-ahead) and from the images catalogued or
  staged for its board type, never from a list per board; without `confirm=apply` it says that,
  with a reason per verb, and runs nothing. The verbs run read-only first, then the writes undone within the run (each after the
  backup that undoes it: a reboot, a no-op apply, the region backup written back, a snapshot and
  the switch back, an update to a second image and back, the key restore planned), then only
  with `--destructive` the destructive ones each followed by its restore (a config region erased
  then apply and keys-restore; the chip erased then restore-chip). The board is planned at the
  start and the end and the record says whether it came back as it was; a start that is not
  clean or a failed write skips the writes after it, and a board its layout does not let the tool
  write gets the reads alone. The same exercise runs as `tests/hw/test_exercise.py` (one test per
  verb, boards named in `RXRBENCH_HW_BOARDS`) and, on the simulated bench, in the sim tests.

## 0.1.0 (2026-11-02), alpha

- First public version, of alpha quality: the `rxrbench.core` framework and the `mesh`, `linux`,
  `zephyr`, `nuttx` and `launcher` plugin collections. It has rough edges and bugs; verify what
  it does before relying on it (README, "Status: alpha"). Layout keys, registry fields and recipe
  names may change between 0.x versions with no migration.
