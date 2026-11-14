# Hardware tests

Tests that touch a real board, run only when asked for by name and never by `just test`:

```bash
RXRBENCH_HW_BOARDS=t-embed,cyd1 ./venv/bin/pytest -q tests/hw            # read what is there
RXRBENCH_HW_BOARDS=cyd1 RXRBENCH_HW_ENTER=cyd1 ./venv/bin/pytest -q tests/hw   # may reset into Launcher
RXRBENCH_HW_RECORD=1 ...                                                 # keep the printouts as fixtures
```

A board named in `RXRBENCH_HW_BOARDS` must resolve through devreg on this machine and have its
broker running; a board that does not is skipped and said. A board in Launcher is read whole. A
board in its app is left alone unless it is also named in `RXRBENCH_HW_ENTER`: then it is reset
into Launcher by its port's modem lines (a power-on only on a UART bridge; a native-USB board
cannot be entered this way and the test says so) and its app is booted again afterwards. Nothing
is ever written to a board here. With `RXRBENCH_HW_RECORD=1` the printouts are kept under
`tests/fixtures/launcher/<board>-<version>/` with the WebUI password and the MAC replaced, for
the parsers' unit tests. Tell whoever shares the bench before running these: a board in its app is reset.

## The board exercise

`tests/hw/test_exercise.py` runs `tools/exercise.py` once per board named in `RXRBENCH_HW_BOARDS`
(the layout in `RXRBENCH_HW_LAYOUT`) and gives one test per verb: a verb that does not apply to
the board is a skip with its reason, a verb that ran passes or fails on what the board said. It
writes to the board: every reversible verb runs (a reboot, a no-op apply, the region backup written
back, a snapshot and the switch back, an update to a second image and back), and with
`RXRBENCH_HW_DESTRUCTIVE=1` the destructive ones too (a config region erased and restored, the chip
erased and restored from this run's backup). `RXRBENCH_HW_SKIP=survey,reboot` leaves verbs out.
Claim the board on whatever your bench shares before running it, and read `just exercise <layout>
<board>` first: without `confirm=apply` it says what would run and why, and runs nothing.

```bash
RXRBENCH_HW_LAYOUT=mybench RXRBENCH_HW_BOARDS=node1 ./venv/bin/pytest -q tests/hw/test_exercise.py
RXRBENCH_HW_LAYOUT=mybench RXRBENCH_HW_BOARDS=node1 RXRBENCH_HW_DESTRUCTIVE=1 ./venv/bin/pytest -q tests/hw/test_exercise.py
```

The record each run leaves is `runs/<time>/exercise/<board>.json`; with `RXRBENCH_HW_RECORD=1` it
is also kept as the site's latest for the board (`<site>/exercise/<board>.json`), which is what
`just matrix` reads to write the site's capability matrix table.
