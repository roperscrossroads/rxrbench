"""The zephyr_mcumgr family: what it knows beyond its row (base.py lists the questions)."""
from . import base

#: The consoles a Zephyr application with MCUmgr may have: its shell. Not every one has a shell,
#: so the row says none and a layout names it for a board whose application does
#: (`bench_console: zephyr-shell`): the board then gets a serial broker on its console port.
CONSOLES = ("zephyr-shell",)


class Family(base.Family):
    #: Its shell's reboot (`kernel reboot cold`, proven by `kernel uptime`). The table is the
    #: family's, not one board's, so the console a layout names never reaches it: the family says
    #: it, the only console it can have. A board without that shell has no broker and is not
    #: offered a reboot (the exercise asks for a console route).
    reboot = base.CONSOLES["zephyr-shell"]["reboot"]

    def board_row(self, hostvars, row):
        console = (hostvars or {}).get("bench_console")
        if console in CONSOLES:
            return {**row, "console": console}
        return row

    def check_layout(self, all_hostvars):
        out = []
        for name, hv in sorted((all_hostvars or {}).items()):
            if (hv.get("firmware") or {}).get("family") != "zephyr_mcumgr":
                continue
            console = hv.get("bench_console")
            if console is not None and console not in CONSOLES:
                out.append(f"{name}: bench_console {console!r}: a Zephyr application's console is "
                           f"one of {', '.join(CONSOLES)}")
        return out
