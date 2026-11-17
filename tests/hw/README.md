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

## The hawkBit route

`tests/hw/test_hawkbit_route.py` runs `apply.yml` on a layout that pins `routes: {flash: hawkbit}`
for one device, against a real server:

```bash
RXRBENCH_HW_LAYOUT=mybench RXRBENCH_HW_BOARDS=node1 \
RXRBENCH_HW_HAWKBIT=http://updates.example.org:8080 RXRBENCH_HW_HAWKBIT_AUTH=bench:... \
  ./venv/bin/pytest -q tests/hw/test_hawkbit_route.py                  # the plan: writes nothing
... RXRBENCH_HW_APPLY=1 ./venv/bin/pytest -q tests/hw/test_hawkbit_route.py    # the delivery too
```

The plan reads the device and the server and must leave the server's record of the target as
it was. With `RXRBENCH_HW_APPLY=1` the device takes its declared bundle through the server:
the run must end with the device on it, the server's record saying success once the device's
client has reported, and a second run finding nothing to do. The device needs a hawkBit client
that polls; for an image that has no timer for it yet, `RXRBENCH_HW_POLL` is a command the test
runs every few seconds in its place (for example an ssh that runs the client once).
`RXRBENCH_HW_BUNDLE` names a bundle to deliver in place of the layout's declared one. The login
is taken from the environment and written nowhere.

A server the device has never been on is fine when the layout declares the device's token
(`bench_hawkbit_token`) and its client's config: the plan then says the run will enroll the
device, and the apply does.

**A failure, too.** `RXRBENCH_HW_BAD_LAYOUT` names a second layout of the same device that
declares an image made to fail on it (one that faults on its test boot, a bundle whose slot
fails its health gate). The apply of it must fail, the server's record must say failure once
the device's client has reported, and the device must be back on its declared image by itself.
A client that reports before it knows, or one that calls a swap back a success, fails this.

**With no server at all.** `RXRBENCH_HW_HAWKBIT=standin:<address>:<port>` has the test start
the plugin's stand-in server (`rxrbench.hawkbit`'s `sim/fakeserver.py`) on that address of
this machine for as long as it runs, and needs no login:

```bash
RXRBENCH_HW_LAYOUT=mybench-updates RXRBENCH_HW_BAD_LAYOUT=mybench-updates-bad RXRBENCH_HW_BOARDS=node1 \
RXRBENCH_HW_HAWKBIT=standin:192.0.2.10:8080 RXRBENCH_HW_APPLY=1 \
  ./venv/bin/pytest -q tests/hw/test_hawkbit_route.py
```

The address is the bench host's on the boards' network, the one their client config names. The
stand-in starts empty every time and checks tokens, so the layout must declare each device's
token. This is the form a bench host can run by itself, on a schedule, with nothing else up.

