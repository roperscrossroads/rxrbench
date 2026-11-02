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
