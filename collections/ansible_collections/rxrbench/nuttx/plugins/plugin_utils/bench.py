"""The NuttX plugin: Apache NuttX devices through NSH, their configuration kept as files.

    nuttx_nsh   known by what it says to `uname -a` (release, build, machine), spoken to
                through NSH (whose echo and prompt the broker's marker read takes as it takes
                Zephyr's); its storage mounted and its declared files written through the
                shell, secret ones kept out of the log; its reboot a power-off, which the
                bench answers by starting the board again

core's plugin_utils/families says what a row's fields are. NuttX's own `sim:nsh` is the PC
bench's board of it (docs/SIMULATED.md; a real board is reached the same way.
"""
from .families import nuttx_nsh

FAMILIES = {
    "nuttx_nsh": {"vocabulary": "files", "config_via": "console", "console": "zephyr-shell",
                  "state_command": "uname -a", "plugin": "nuttx_nsh"},
}

PLUGINS = {"nuttx_nsh": nuttx_nsh.Family()}
