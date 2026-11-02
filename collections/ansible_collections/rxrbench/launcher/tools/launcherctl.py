#!/usr/bin/env python3
"""launcherctl.py: drive an ESP32's Launcher WebUI from a shell.

    launcherctl.py --board NAME status                 version, SD, slots, free flash
    launcherctl.py facts                               the same as JSON (launcher_facts)
    launcherctl.py wait [--gone] [--timeout S]         until Launcher answers (or stops)
    launcherctl.py ls [FOLDER]                         the SD card
    launcherctl.py get SDPATH PATH                     download a file
    launcherctl.py put PATH [FOLDER]                   upload a file (skipped if the same
                                                       name and size are there; --force)
    launcherctl.py rm SDPATH
    launcherctl.py boot LABEL                          start an installed app now
    launcherctl.py reboot
    launcherctl.py install IMAGE.bin [--from-sd]       install into a new slot (by upload,
                                                       or from a file already on the SD)
    launcherctl.py part show                           the partition manager's table
    launcherctl.py part ACTION key=value ... --confirm resize/create/delete/format/discard/
                                                       backup/restore edit a draft; apply
                                                       writes it and reboots

Board: --board NAME, a registered board whose wifi route devreg resolves and guards (the
registry must say the address is that board), as every write to hardware is resolved by name.
--url http://host reads a WebUI by address (status, facts, ls, get, wait) and is refused for a
write. Password: a file only its owner may read, holding `user:password`
or the bare password (--auth-file, default $XDG_CONFIG_HOME/rxrbench/launcher.auth); never
an argument or an environment variable, which other processes can read. The session token is kept per host under
$XDG_CACHE_HOME/rxrbench/launcher/ (mode 600), so repeated calls do not log in again and do
not log a browser out each time. Nothing secret is printed.

Every write is read back: an upload is listed, an install is a new slot in the table, a boot
is Launcher going away. Partition edits other than `show` and `discard` need --confirm: an
apply moves partitions around and a delete loses what a slot or a data partition held.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *[".."] * 4)))
from ansible_collections.rxrbench.launcher.plugins.module_utils import webui  # noqa: E402

GUARDED = {"resize", "create", "delete", "format", "apply", "backup", "restore"}
SLOT_ALIGN = 0x10000


def xdg(var, fallback):
    return os.environ.get(var) or os.path.expanduser(fallback)


def session_file(ui):
    return os.path.join(xdg("XDG_CACHE_HOME", "~/.cache"), "rxrbench", "launcher",
                        f"{ui.host}_{ui.port}.session")


DEVREG = os.path.abspath(os.path.join(os.path.dirname(__file__), *[".."] * 5, "devreg", "devreg.py"))
READS = {"status", "facts", "wait", "ls", "get"}


def board_url(name, devreg):
    """The WebUI of a registered board: its wifi route's address, resolved by name and guarded
    (the registry must say that address is that board), never typed. Exits when the registry
    has no such route."""
    cmd = [sys.executable, devreg, "resolve", "--route", "wifi", name]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    addr = r.stdout.strip()
    if r.returncode != 0 or not addr:
        sys.exit(f"launcherctl: {name}: no wifi route in the registry ({(r.stdout + r.stderr).strip()[-200:]})")
    g = subprocess.run([sys.executable, devreg, "guard", "--route", "wifi", name, addr],
                       capture_output=True, text=True, check=False)
    if g.returncode != 0:
        sys.exit(f"launcherctl: devreg guard refuses {addr} for {name}: {(g.stdout + g.stderr).strip()[-200:]}")
    return f"http://{addr}"


def connect(args):
    if args.board:
        args.url = board_url(args.board, args.devreg)
    elif args.cmd not in READS or (args.cmd == "part" and args.action != "show"):
        sys.exit("launcherctl: a write names a registered board (--board NAME); --url is for reading")
    user, password = args.user, None
    auth = args.auth_file or os.path.join(xdg("XDG_CONFIG_HOME", "~/.config"), "rxrbench",
                                          "launcher.auth")
    if os.path.exists(auth):
        user, password = webui.read_secret_file(auth)
    ui = webui.WebUI(args.url, user, password)
    try:
        with open(session_file(ui), encoding="utf-8") as fh:
            ui.session = fh.read().strip() or None
    except OSError:
        pass
    return ui


def keep_session(ui):
    if not ui.session:
        return
    path = session_file(ui)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(ui.session)


def need_launcher(ui):
    if not ui.ping():
        sys.exit(f"launcherctl: no Launcher answers at {ui.host}:{ui.port} "
                 "(off, off WiFi, or running one of its apps)")


def human(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def cmd_status(ui, _args):
    f = webui.facts(ui)
    if not f["launcher"]["running"]:
        print(f"no Launcher at {ui.host}:{ui.port}")
        return 1
    sd = f["sd"]
    print(f"Launcher {f['launcher']['version']} at {ui.host}:{ui.port}")
    print(f"SD       {human(sd.get('usedBytes'))} used of {human(sd.get('totalBytes'))}")
    if not f["launcher"]["logged_in"]:
        print("apps     " + ", ".join(a["name"] for a in f["apps"]) + "   (no password: no slot table)")
        return 0
    print(f"flash    {human(f['flash']['size'])}, {human(f['flash']['free'])} unallocated"
          + ("   UNAPPLIED EDITS in the partition manager" if f["flash"]["dirty"] else ""))
    print(f"{'slot':<17} {'subtype':<8} {'offset':>9} {'size':>10}  data")
    for s in f["slots"]:
        print(f"{s['label']:<17} {s['subtype'] or '':<8} {s['offset']:>#9x} {human(s['size']):>10}  "
              f"{','.join(s['data']) or '-'}" + ("   <- running" if s["running"] else ""))
    if f["nvs"] is not None:
        print("nvs      " + ", ".join(f"{ns}({n})" for ns, n in sorted(f["nvs"].items())))
    return 0


def cmd_facts(ui, _args):
    print(json.dumps(webui.facts(ui), indent=2))
    return 0


def cmd_wait(ui, args):
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if ui.ping() != args.gone:
            print("gone" if args.gone else "Launcher answers")
            return 0
        time.sleep(2)
    print("timed out", file=sys.stderr)
    return 1


def cmd_ls(ui, args):
    need_launcher(ui)
    for e in ui.list_files(args.folder):
        print(f"{'d' if e['dir'] else '-'} {e['size'] or '':>10}  {e['name']}")
    return 0


def cmd_get(ui, args):
    need_launcher(ui)
    data = ui.download(args.sdpath)
    with open(args.path, "wb") as fh:
        fh.write(data)
    print(f"{args.sdpath} -> {args.path} ({len(data)} bytes)")
    return 0


def listed(ui, folder, name):
    return next((e for e in ui.list_files(folder) if e["name"] == name and not e["dir"]), None)


def same_size(entry, size):
    """The listing rounds sizes up to a whole unit, so "the same size" is the same text: a
    file that changed by less than its unit looks unchanged (--force sends it anyway)."""
    return entry is not None and entry["size"] == webui.human_size(size)


def cmd_put(ui, args):
    need_launcher(ui)
    with open(args.path, "rb") as fh:
        data = fh.read()
    name = args.name or os.path.basename(args.path)
    folder = args.folder.rstrip("/") or "/"
    if not args.force and same_size(listed(ui, folder, name), len(data)):
        print(f"{folder.rstrip('/')}/{name} is there at that size: unchanged (--force to send)")
        return 0
    ui.upload(data, name, folder)
    if not same_size(listed(ui, folder, name), len(data)):
        sys.exit(f"launcherctl: {name} is not listed at its size after the upload")
    print(f"{args.path} -> {folder.rstrip('/')}/{name} ({len(data)} bytes)")
    return 0


def cmd_rm(ui, args):
    need_launcher(ui)
    ui.delete_file(args.sdpath)
    folder, _sep, name = args.sdpath.rpartition("/")
    if listed(ui, folder or "/", name):
        sys.exit(f"launcherctl: {args.sdpath} is still listed")
    print(f"deleted {args.sdpath}")
    return 0


def cmd_boot(ui, args):
    need_launcher(ui)
    labels = [s["label"] for s in webui.slots(ui.partitions())]
    if args.label not in labels:
        sys.exit(f"launcherctl: no slot {args.label!r} (slots: {', '.join(labels)})")
    ui.boot_app(args.label)
    gone = wait_gone(ui, 30)
    print(f"booting {args.label}" + ("" if gone else " (Launcher still answers after 30 s)"))
    return 0 if gone else 1


def wait_gone(ui, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(2)
        if not ui.ping():
            return True
    return False


def cmd_reboot(ui, _args):
    need_launcher(ui)
    ui.reboot()
    print("rebooting" if wait_gone(ui, 30) else "Launcher still answers after 30 s")
    return 0


def cmd_install(ui, args):
    need_launcher(ui)
    before = ui.partitions()
    labels = {s["label"] for s in webui.slots(before)}
    if args.from_sd:
        entry = listed(ui, os.path.dirname(args.image) or "/", os.path.basename(args.image))
        size = entry and entry["bytes"]
        if not entry:
            sys.exit(f"launcherctl: {args.image} is not on the SD card")
        need = -(-(size or 0) // SLOT_ALIGN) * SLOT_ALIGN
    else:
        with open(args.image, "rb") as fh:
            data = fh.read()
        manifest = webui.ota_manifest(data, os.path.basename(args.image))
        app = manifest["parts"][0]
        need = -(-app["copySize"] // SLOT_ALIGN) * SLOT_ALIGN
        if len(manifest["parts"]) > 1:
            print("the file is a merged image: its data partitions come too: "
                  + ", ".join(p["label"] for p in manifest["parts"][1:]))
    free = webui.free_bytes(before)
    if need > free:
        sys.exit(f"launcherctl: the slot needs {human(need)}, {human(free)} is unallocated "
                 "(delete or shrink a slot first: `part ...`)")
    if args.from_sd:
        ui.install_from_sd(args.image)
    else:
        ui.install_upload(data, os.path.basename(args.image))
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        new = [s for s in webui.slots(ui.partitions()) if s["label"] not in labels]
        if new:
            s = new[0]
            print(f"installed into slot {s['label']} at {s['offset']:#x}, {human(s['size'])}; "
                  f"start it with: boot {s['label']}")
            return 0
        time.sleep(3)
    sys.exit("launcherctl: no new slot appeared (see the device's screen)")


def cmd_part(ui, args):
    need_launcher(ui)
    if args.action == "show":
        print(json.dumps(ui.partitions(), indent=2))
        return 0
    if args.action in GUARDED and not args.confirm:
        sys.exit(f"launcherctl: `part {args.action}` changes the flash map: add --confirm")
    params = dict(kv.split("=", 1) for kv in args.params)
    reply = ui.partition_action(args.action, **params)
    if args.action == "apply":
        print("applying: Launcher rewrites the table (and moves partitions) and reboots")
        return 0
    print(json.dumps({"dirty": reply.get("dirty"), "slots": webui.slots(reply),
                      "free": webui.free_bytes(reply)}, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--board", help="a registered board: its wifi route, resolved and guarded by devreg")
    who.add_argument("--url", help="a WebUI by address, http://host[:port]: reads only")
    ap.add_argument("--devreg", default=DEVREG, help="devreg.py (the repository's by default)")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--auth-file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("facts")
    p = sub.add_parser("wait")
    p.add_argument("--gone", action="store_true")
    p.add_argument("--timeout", type=float, default=120)
    p = sub.add_parser("ls")
    p.add_argument("folder", nargs="?", default="/")
    p = sub.add_parser("get")
    p.add_argument("sdpath")
    p.add_argument("path")
    p = sub.add_parser("put")
    p.add_argument("path")
    p.add_argument("folder", nargs="?", default="/")
    p.add_argument("--name")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("rm")
    p.add_argument("sdpath")
    p = sub.add_parser("boot")
    p.add_argument("label")
    sub.add_parser("reboot")
    p = sub.add_parser("install")
    p.add_argument("image")
    p.add_argument("--from-sd", action="store_true")
    p.add_argument("--timeout", type=float, default=300)
    p = sub.add_parser("part")
    p.add_argument("action", choices=sorted(GUARDED | {"show", "discard"}))
    p.add_argument("params", nargs="*")
    p.add_argument("--confirm", action="store_true")
    args = ap.parse_args(argv)
    ui = connect(args)
    try:
        return globals()[f"cmd_{args.cmd}"](ui, args)
    except webui.LauncherError as e:
        sys.exit(f"launcherctl: {e}")
    finally:
        keep_session(ui)


if __name__ == "__main__":
    sys.exit(main())
