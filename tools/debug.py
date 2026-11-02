#!/usr/bin/env python3
"""debug.py <layout> <board> start|attach|stop|status [gdb -ex args...]: JTAG on an ESP32-S3 board.

Over the same USB cable as the console: the ESP32-S3's USB Serial/JTAG has the console on
interfaces 0+1 (the board's broker holds them) and JTAG on interface 2, which OpenOCD claims
beside it. No probe, no wiring, and the console stays with its broker.

  start   OpenOCD on the board's bench host, pinned to this board by its USB serial (from the
          registry: every ESP32-S3 on its native USB-JTAG is 303a:1001, and an unpinned adapter takes the first one)
  attach  GDB here, with the symbols of exactly the image the board runs (staged beside the
          store when the image is staged), refused unless the board reports that build. The session
          ends with `monitor resume` + `detach`, then the board must answer on its console
  stop    stop OpenOCD on the board's host
  status  is OpenOCD running there, and on which board

Rules for a session (the reasons: docs/HARDWARE.md):
- `hbreak` only, never a software breakpoint or `finish`;
- no GDB inferior calls (`p func()`);
- at a function-entry breakpoint, frame #0's arguments are not yet valid;
- halting perturbs timing: a breakpoint hitting is not evidence the fault is live;
- the session ends with `monitor resume` before `detach`, and the console is checked after;
- OpenOCD is stopped before the board is flashed (`just apply` would fight it for the device).
"""
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402
from ansible_collections.rxrbench.core.plugins.plugin_utils import families  # noqa: E402

site.load_env()
GDB = os.environ.get("RXRBENCH_GDB")          # the workstation's GDB for the chip (.env)
OCD = os.environ.get("RXRBENCH_OPENOCD")      # an OpenOCD tree on the bench host: bin/, share/
GDB_PORT = 3333


def setting(name, value):
    if not value:
        die(f"{name} is not set: .env.example says what it is", 2)
    return value


def find_ocd():
    """A shell line listing the OpenOCD process started from the configured tree."""
    base = re.escape(os.path.basename(setting("RXRBENCH_OPENOCD", OCD).rstrip("/")))
    return f"ps -eo pid,args | awk '/{base}\\/bin\\/openoc[d]/ && $2 !~ /bash/ {{print}}'"


def die(msg, rc=2):
    print(f"debug: {msg}", file=sys.stderr)
    sys.exit(rc)


def inventory(layout):
    d = subprocess.run([sys.executable, os.path.join(REPO, "tools", "where.py"), layout],
                       capture_output=True, text=True, check=True, cwd=REPO).stdout.strip()
    out = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-inventory"), "-i", d,
                          "--list"], capture_output=True, text=True, check=True, cwd=REPO)
    return json.loads(out.stdout)


def plain(v):
    """A value as ansible-inventory --list prints it, without its {"__ansible_unsafe": ...}
    wrapper (a string from a template comes out wrapped)."""
    if isinstance(v, dict):
        return v["__ansible_unsafe"] if set(v) == {"__ansible_unsafe"} else \
            {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


def board_facts(inv, board):
    hv = plain(inv.get("_meta", {}).get("hostvars", {}))
    if board not in hv:
        die(f"{board} is not in this layout")
    host = next((g[len("host_"):] for g, v in inv.items()
                 if g.startswith("host_") and board in (v or {}).get("hosts", [])), None)
    if not host:
        die(f"{board} has no bench host in this layout")
    user = hv.get(host, {}).get("ansible_user")
    return hv[board], (f"{user}@{host}" if user else host), host


def usb_jtag_serial(board):
    """The board's USB serial (its MAC) from the registry's by-id match: what pins OpenOCD."""
    import yaml  # pylint: disable=import-outside-toplevel
    with open(site.registry_path()) as f:
        dev = (yaml.safe_load(f)["devices"] or {}).get(board) or {}
    by_id = (dev.get("match") or {}).get("serial_by_id") or ""
    m = re.search(r"USB_JTAG_serial_debug_unit_([0-9A-Fa-f:]{17})-if", by_id)
    return m.group(1) if m else None


def ssh(target, cmd, timeout=60):
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", target, cmd], capture_output=True,
                       text=True, timeout=timeout, check=False)
    if r.returncode == 255:      # ssh itself failed: say why, not "no output"
        die(f"ssh {target}: {r.stderr.strip()[-300:]}", 255)
    return r.returncode, r.stdout.strip()


def broker(target, board, *args, timeout=30):
    rc, out = ssh(target, " ".join(["~/rxrbench/bin/brokerctl.py", *args[:1], board,
                                    *[f"'{a}'" for a in args[1:]]]), timeout)
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": out or f"rc {rc}"}


def board_state(target, board, command):
    """The board's state line as a dict (asked with its family's state command), or None."""
    out = broker(target, board, "exec", command).get("output") or ""
    m = re.search(r"~S(\{.*\})\*[0-9a-f]{4}", out)
    try:
        return json.loads(m.group(1)) if m else None
    except ValueError:
        return None


def running_build(target, board, command):
    st = board_state(target, board, command)
    return st.get("build") if st else None


def symbols_for(family, st):
    """(elf, build) of the staged image the board runs: the same build, the same value of the
    family's identity key, and each of the family's features in the image exactly when the
    board's state line has it. The symbols are the one ELF file staged beside the image
    (`.build/debug/<family>/<image>/`), whatever the build named it."""
    ident = families.of(family)["identity_key"]
    feats = families.plugin(family).features
    store = os.path.join(REPO, ".build", "store", family)
    for name in sorted(os.listdir(store)) if os.path.isdir(store) else []:
        try:
            with open(os.path.join(store, name, "manifest.json")) as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if m.get("build") == st.get("build") and (not ident or m.get(ident) == st.get(ident)) and \
                all((f in (m.get("features") or [])) == (f in st) for f in feats):
            dbg = os.path.join(REPO, ".build", "debug", family, name)
            elves = sorted(e for e in os.listdir(dbg) if e.endswith(".elf")) if os.path.isdir(dbg) else []
            if len(elves) == 1:
                return os.path.join(dbg, elves[0]), m["build"]
    return None, None


def main(argv):
    if len(argv) < 3 or argv[2] not in ("start", "attach", "stop", "status"):
        die(__doc__.splitlines()[0])
    layout, board, action, extra = argv[0], argv[1], argv[2], argv[3:]
    hv, target, host = board_facts(inventory(layout), board)

    if action == "status":
        _rc, out = ssh(target, find_ocd() + "; ss -ltn | grep -E ':(3333|4444)' || true")
        print(out or "OpenOCD is not running")
        return
    if action == "stop":
        _rc, out = ssh(target, f"pid=$({find_ocd()} | awk '{{print $1}}' | head -1); "
                               "if [ -n \"$pid\" ]; then kill $pid; sleep 2; echo stopped $pid; "
                               "else echo not running; fi")
        print(out)
        return

    serial = usb_jtag_serial(board)
    if not serial:
        die(f"{board} is not an ESP32-S3 USB-JTAG board in the registry (no "
            "USB_JTAG_serial_debug_unit by-id match): nothing to pin OpenOCD to")

    if action == "start":
        _rc, out = ssh(target, find_ocd())
        if out:
            die(f"OpenOCD is already running on {host}:\n{out}\nstop it first "
                f"(just debug {layout} {board} stop): a second one collides on 3333/4444", 3)
        log = f"~/rxrbench/logs/openocd-{board}.log"
        # `setsid -f` forks OpenOCD off with its own session and no subshell between: a
        # backgrounded `cd ... && setsid ... &` kept the ssh session open (and hung it).
        ocd = setting("RXRBENCH_OPENOCD", OCD)
        rc, out = ssh(target, f"test -x {ocd}/bin/openocd || {{ echo 'no OpenOCD at {ocd}'; "
                              f"exit 1; }}; setsid -f {ocd}/bin/openocd "
                              f"-s {ocd}/share/openocd/scripts -f interface/esp_usb_jtag.cfg "
                              f"-c 'adapter serial {serial}' -c 'bindto 0.0.0.0' "
                              f"-f target/esp32s3.cfg > {log} 2>&1 < /dev/null; sleep 8; "
                              f"tail -5 {log}", timeout=90)
        print(f"== {board} ({serial}) on {host}\n{out}")
        _rc, listening = ssh(target, "ss -ltn | grep -c ':3333 '")
        if rc != 0 or listening.strip() in ("", "0"):
            die("OpenOCD did not come up listening for GDB (see the log above)", 4)
        return

    # attach: the symbols of exactly the image the board runs, found by what it says it runs
    # among the staged images.
    family = hv.get("bench_family")
    if not family:
        die(f"{board} declares no firmware family: no image store to find its symbols in")
    command = families.of(family)["state_command"]
    if not command:
        die(f"{board}: {family} firmware prints no state line to find its image by")
    st = board_state(target, board, command)
    if st is None:
        die(f"{board} does not answer with its state line: does it run {family} firmware?")
    elf, have = symbols_for(family, st)
    if elf is None:
        die(f"{board} runs build {st.get('build')}, and no staged image of that has its symbols (one .elf file) "
            f"under .build/debug/{family}/: stage it with its debug files")
    print(f"== {board} runs {have}; symbols {elf}")
    want = st.get("build")
    subprocess.run([setting("RXRBENCH_GDB", GDB), elf, "-batch", "-ex", "set pagination off", "-ex", "set confirm off",
                    "-ex", f"target extended-remote {host}:{GDB_PORT}", *extra,
                    "-ex", "monitor resume", "-ex", "detach"], check=False)
    # A halted board is silent, and silence is what a quiet success looks like too: ask it.
    for _ in range(10):
        time.sleep(1.5)
        if running_build(target, board, command) == want:
            print(f"== {board} answers on its console: running")
            return
    die(f"{board} does not answer on its console since the detach: it may be left HALTED. "
        f"`just debug {layout} {board} attach -ex 'monitor resume'`, or reset it "
        "(`just restore-chip` is not needed: any esptool chip-id resets it)", 5)


if __name__ == "__main__":
    main(sys.argv[1:])
