#!/usr/bin/env python3
"""exercise.py <layout> <board> [--destructive] [--skip verb,..] [--list] [--json]: one board through
every verb of the tool that applies to it, and a record of what happened. `--record` keeps the
record as the site's latest for the board (`<site>/exercise/<board>.json`), the file
`tools/matrix.py` reads.

What applies is derived from what the inventory publishes for the board (`ansible-inventory
--host`), from the firmware catalog and from the images staged in the store, never from a list
per board, and no board or family is named here. The verbs run read-only first, then the writes
that are undone within the run (each preceded by the backup or record that undoes it), then,
only with `--destructive`, the destructive ones, each followed by its restore (the family switch
takes the other family's declaration from the site's `alternatives/<board>/<name>.yml`). The board is
planned at the start and at the end, and the record says whether it came back as it was. A verb
that does not apply is recorded with its reason; a write that fails stops the writes after it,
and the final plan still runs. Every write is one of the tool's own plays, through its gate,
limited to the one board. `--list` prints what would run and stops.

The record is `runs/<time>/exercise/<board>.json`; each verb's play writes under
`runs/<time>/exercise/<board>/<nn>-<verb>/`.

`--ci` is the run a CI job makes: only a board its layout marks `bench_ci: {run: true}` is
exercised (any other is refused, nothing run), and the destructive verbs run only where it also
says `destructive: true`; `--destructive` is not taken with it. `tools/ci_boards.py` lists the
marked boards of a layout. A marked board that is not plugged in to this host (devreg finds it
absent) is not run and not recorded, and the exit code is 3 (NOT_PRESENT), so a job can tell a
board unplugged from one that failed; one devreg cannot tell apart from another is a failure.
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  pylint: disable=unused-import,wrong-import-position  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402  pylint: disable=wrong-import-position

VENV = os.path.join(REPO, "venv", "bin")
RUNS = os.path.join(REPO, "runs")
PLAYBOOKS = os.path.join("collections", "ansible_collections", "rxrbench", "core", "playbooks")
#: The exit code of a CI run whose board is not plugged in to this host: nothing ran.
NOT_PRESENT = 3
#: The registry's own tool, run against the site's registry.
DEVREG = os.path.join(REPO, "devreg", "devreg.py")
#: Kinds, in the order the exercise runs them.
READ, REVERSIBLE, DESTRUCTIVE, NOT_RUN = "read", "reversible", "destructive", "not run"
#: Results.
PASS, FAIL, SKIP = "pass", "fail", "skip"
#: Reversible verbs that write to the bench host or the site only, never to the board: they run
#: whatever the board's state, since nothing of theirs needs undoing.
HOST_ONLY = {"backup-regions", "backup-chip", "keys-export"}
#: What a verb needs this run to have done first, so a selected verb brings it along.
NEEDS = {"restore-regions": ("backup-regions",), "erase-region": ("backup-regions",),
         "erase-chip": ("backup-chip",), "family-switch": ("backup-chip",),
         "update-there-and-back": ("backup-chip",)}
#: Always run: the start plan (whether the board matches its layout) and the end (came-back).
ALWAYS = {"plan"}
#: The flashers whose deep survey resets the chip into its ROM loader and back (core's own names).
DEEP_RESETS = ("esptool", "esp-image")
#: The backup play's "Where it went" line: `<board>: regions <stamp> (...); chip <stamp> (...); ...`.
#: The stamps are this run's own backups, which every restore of the run writes back by name: a
#: region backup's name carries the build it was read from (`20261115T144156Z-8d9b1b18`).
BACKUP_STAMP = re.compile(r"\b(regions|chip) (\d{8}T\d{6}Z(?:-[0-9A-Za-z]+)?) \(")
RECAP = re.compile(r"^(\S+)\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)\s+skipped=(\d+)",
                   re.M)


# ---- what applies ------------------------------------------------------------------------------

def _bearers(hv):
    return {r.get("bearer") for r in (hv.get("bench_routes") or {}).values() if isinstance(r, dict)}


def _spec(hv):
    fam = hv.get("bench_family")
    table = hv.get("bench_families") or {}
    return table.get(fam) or {} if isinstance(table, dict) else {}


def _console(hv, spec):
    """The console the tool speaks to the board through, as the inventory decides it: the family's
    row, which the family's plugin may complete from the layout (a Zephyr MCUmgr application's
    `bench_console: zephyr-shell`). None: its port, if any, has no broker."""
    if spec.get("console"):
        return spec["console"]
    try:
        from ansible_collections.rxrbench.core.plugins.plugin_utils import families  # pylint: disable=import-outside-toplevel
        return families.plugin(hv.get("bench_family")).board_row(hv, spec).get("console")
    except Exception:  # pylint: disable=broad-except  (no plugins here: the row alone)
        return None


def _config_regions(hv):
    """The board's config regions by name: the family's, or the ranges declared for the chip."""
    names = [r[0] for r in _spec(hv).get("regions") or [] if r]
    names += [(r or {}).get("label") for r in hv.get("bench_flash_regions") or [] if (r or {}).get("role") == "config"]
    return [n for n in names if n]


def _device_secret(hv):
    kinds = hv.get("bench_secret_kinds") or []
    return [k for k in kinds if isinstance(k, dict) and k.get("origin") == "device"]


def _images_for(hv, images):
    """The images written to this board's type, by family: {family: {name: entry}}. `images`
    holds the catalog's entries (`board_tags`) and the store's staged builds (`board_tag`) alike."""
    tag = hv.get("bench_board_tag")
    out = {}
    for name, entry in (images or {}).items():
        if not isinstance(entry, dict) or not tag:
            continue
        tags = list(entry.get("board_tags") or []) + ([entry["board_tag"]] if entry.get("board_tag") else [])
        if tag in tags:
            out.setdefault(entry.get("family"), {})[name] = entry
    return out


def staged_images(store):
    """The images staged under the store (`<store>/<dir>/<name>/manifest.json`), as entries with
    `family`, `board_tag` and what tells them apart (`class`, `build`, `version`, `features`):
    what a family built here has to update to and from."""
    out = {}
    if not os.path.isdir(store):
        return out
    for sub in sorted(os.listdir(store)):
        d = os.path.join(store, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            m = os.path.join(d, name, "manifest.json")
            if not os.path.isfile(m):
                continue
            try:
                with open(m) as fh:
                    man = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(man, dict) and man.get("family"):
                out[name] = {k: man.get(k) for k in ("family", "board_tag", "class", "build", "version", "features", "app")}
                out[name]["staged"] = True
    return out


def other_image(hv, images, accept=None):
    """The declaration of a second image for the board, or None: another image of its type (a
    catalogued one first, then a staged build), or for a family built here another staged class
    with the same features. An image counts when the board's family takes the declaration
    (`accept(fw)`, the layout's own rule, `Family.image`), when it records the board's family, or
    when it is a build staged from the same application as the board's image (the same firmware
    at another version, whatever family name the build recorded). When the board's image says what
    it provides (`features` in its catalog entry), a second image must provide all of it: a layout
    that checks its secrets with a command one image has fails on an image without it."""
    fw = hv.get("firmware") or {}
    by_family = _images_for(hv, images)
    family = hv.get("bench_family")

    current = (images or {}).get(fw.get("image")) or {}

    def ok(fw2, entry):
        if accept is not None and accept(fw2):
            return True
        if entry.get("family") == family:
            return True
        # a build staged from the same application for the same board type: the same firmware
        # at another version, whatever family name its build recorded
        return bool(entry.get("staged") and current.get("staged") and entry.get("app")
                    and entry.get("app") == current.get("app"))

    if fw.get("class") is not None:
        want = sorted(fw.get("features") or [])
        for entry in (by_family.get(family) or {}).values():
            if entry.get("staged") and entry.get("class") is not None and entry["class"] != fw["class"] \
                    and sorted(entry.get("features") or []) == want:
                fw2 = {**fw, "class": entry["class"]}
                if ok(fw2, entry):
                    return fw2
        return None
    need = set(current.get("features") or [])
    pool = [(n, e) for entries in by_family.values() for n, e in entries.items()
            if n != fw.get("image") and need <= set(e.get("features") or [])]
    for staged in (False, True):
        for name, entry in pool:
            if bool(entry.get("staged")) == staged and ok({**fw, "image": name}, entry):
                return {**fw, "image": name}
    return None


def second_image_gap(hv, images, accept=None):
    """Why no second image qualifies, when the board's image declares features no other image of
    its family and type has all of: the features missing (`no second image with digest`); else ""."""
    fw = hv.get("firmware") or {}
    need = set(((images or {}).get(fw.get("image")) or {}).get("features") or [])
    if not need or fw.get("class") is not None or other_image(hv, images, accept):
        return ""
    family = hv.get("bench_family")
    others = [e for f, entries in _images_for(hv, images).items() for n, e in entries.items()
              if n != fw.get("image") and (e.get("family") == family or f == family)]
    if not others:
        return ""
    best = min(others, key=lambda e: len(need - set(e.get("features") or [])))
    return "no second image of the family for this board type with " + ", ".join(
        sorted(need - set(best.get("features") or [])))


def alternatives(site_dir, board):
    """The site's alternative declarations for the board, `<site>/alternatives/<board>/<name>.yml`
    (a host_vars override: `firmware` and the family's own block, the go-aheads it needs), by
    name: [{name, path, vars}]. None when the site keeps none."""
    import yaml  # pylint: disable=import-outside-toplevel
    out = []
    for path in sorted(glob.glob(os.path.join(site_dir or "", "alternatives", board, "*.yml"))):
        with open(path) as fh:
            v = yaml.safe_load(fh) or {}
        if isinstance(v, dict) and isinstance(v.get("firmware"), dict):
            out.append({"name": os.path.splitext(os.path.basename(path))[0], "path": path, "vars": v})
    return out


def other_family(hv, images, alts):
    """The first alternative that declares another family whose image (by name, or by class for a
    family built here) the catalog or the store has for this board type; None without one."""
    family = hv.get("bench_family")
    by_family = _images_for(hv, images)
    for a in alts or ():
        fw = a["vars"]["firmware"]
        fam = fw.get("family")
        if not fam or fam == family:
            continue
        entries = by_family.get(fam) or {}
        if fw.get("image") in entries or (fw.get("class") is not None
                                          and any(e.get("class") == fw["class"] for e in entries.values())):
            return a
    return None


def _has_chip(hv):
    return "chip" in set(hv.get("bench_can") or []) or bool((_spec(hv).get("chip_tasks") or {}).get("backup"))


def applicable(hv, images=None, accept=None, alts=()):
    """Every verb of the exercise for the board whose published variables are `hv`, in the
    order the exercise runs them: [{verb, kind, applies, reason}]. `images` is the firmware
    catalog's images table plus the store's staged builds, for the verbs that need a second image."""
    can = set(hv.get("bench_can") or [])
    spec = _spec(hv)
    bearers = _bearers(hv)
    via = hv.get("bench_via")
    allowed = bool(hv.get("bench_apply_allowed"))
    family = hv.get("bench_family")
    fw = hv.get("firmware") or {}
    by_family = _images_for(hv, images)
    others = sorted(f for f in by_family if f != family and f)
    chip = _has_chip(hv)
    alt = other_family(hv, images, alts)
    switch_why_not = ("no alternative declares another family for it (<site>/alternatives/<board>/<name>.yml"
                      + (f"; catalogued for its type: {', '.join(others)})" if others else ")"))
    regions = "regions" in can and bool(_config_regions(hv))
    device_secret = _device_secret(hv)
    # a console the tool speaks (the family's row names one, or its plugin does from the layout:
    # a broker owns the port); a bare family's board has a port but no broker (no_console)
    console_or_ssh = via == "ssh" or (via == "console" and bool(_console(hv, spec)))
    diag_why_not = ("the family has no console this tool speaks: its port has no broker" if via == "console"
                    else "no console or ssh to pull diagnostics through")
    second = other_image(hv, images, accept)
    second_why = ("a second image of the family for this board type" if second and "image" in second and second.get("image") != fw.get("image")
                  else "another class of a family built here" if second else "")

    def verb(name, kind, applies, why_not, why=""):
        return {"verb": name, "kind": kind, "applies": bool(applies), "reason": (why if applies else why_not)}

    def write(name, kind, applies, why_not, why=""):
        """A writing verb: refused on a board its layout does not let the tool write."""
        if applies and not allowed:
            return verb(name, kind, False, "the layout gives this board no bench_apply_allowed")
        return verb(name, kind, applies, why_not, why)

    return [
        verb("survey", READ, "usb" in bearers, "no USB route: the survey reads USB devices", "a USB route"),
        verb("facts", READ, hv.get("bench_plugin") and hv.get("bench_vocabulary"),
             "the family has no facts step: no vocabulary", f"its plugin's facts step ({hv.get('bench_vocabulary')})"),
        verb("plan", READ, True, "", "every board is planned"),
        verb("secrets", READ, hv.get("bench_secret_kinds"), "the family declares no secrets", "the family declares secrets"),
        verb("diag", READ, console_or_ssh, diag_why_not, f"diagnostics through its {via}"),
        verb("device-test", READ, hv.get("bench_tests") or hv.get("bench_test_bindings"),
             "the layout declares no device tests and binds no library test for it", "the layout declares tests for it"),
        verb("locate", READ, hv.get("bench_locate"), "the family names no indicator", f"indicator: {hv.get('bench_locate')}"),
        verb("boot", READ, hv.get("bench_bootloader"), "no bootloader declared for it",
             "a bootloader is declared: its plan reads what was last seen"),
        write("reboot", REVERSIBLE, spec.get("reboot") and via == "console",
              "no console reboot in the family's spec", "the family's reboot command"),
        verb("backup-regions", REVERSIBLE, regions,
             "the flasher cannot read regions" if "regions" not in can
             else "no config regions: the family names none and the chip declares no config range",
             "the flasher reads regions and the board has config regions"),
        verb("backup-chip", REVERSIBLE, chip,
             "the flasher cannot read the whole chip and the family has no chip task for it", "the flasher reads the whole chip"),
        write("survey-deep", REVERSIBLE, "usb" in bearers and hv.get("bench_flasher") in DEEP_RESETS,
              "a deep survey resets only a chip read through its ROM loader; this board's is not" if "usb" in bearers
              else "no USB route", "the chip is read through its ROM loader and comes back; after the backups on purpose"),
        verb("keys-export", REVERSIBLE, device_secret, "no secret the device makes", "a secret the device makes is escrowed"),
        write("apply", REVERSIBLE, True, "", "expected to change nothing"),
        write("restore-regions", REVERSIBLE, regions, "no region backup to write back",
              "the backup just taken, expected to leave the board as it is"),
        write("snapshot-switch", REVERSIBLE, chip, "no whole-chip route", "a named snapshot, then switch back to it"),
        write("update-there-and-back", REVERSIBLE, bool(second),
              second_image_gap(hv, images, accept) or "no second image of the family for this board type", second_why),
        write("keys-restore", REVERSIBLE, device_secret and any(k.get("escrow") for k in device_secret),
              "no escrowed device secret to give back", "the escrowed secret, planned; given back after an erase"),
        write("erase-region", DESTRUCTIVE, regions, "no config region to erase", "one config region erased, then apply and keys-restore"),
        write("erase-chip", DESTRUCTIVE, chip, "no whole-chip route", "the chip erased, then restore-chip from this run's backup"),
        write("family-switch", DESTRUCTIVE, bool(alt), switch_why_not,
              f"the site's alternative `{alt['name']}` declares {alt['vars']['firmware'].get('family')}, "
              "an image this board type has; then back by apply, or by restore-chip" if alt else ""),
        verb("first-flash", NOT_RUN, False, "a board's one-time entry, not an exercise"),
        verb("card", NOT_RUN, False, "needs a card in a registered reader; recorded as not run"),
    ]


VERBS = [v["verb"] for v in applicable({"bench_routes": {}, "bench_families": {}})]


# ---- reading the board's data -------------------------------------------------------------------

#: Variables a layout may declare as a template (`bench_tests: "{{ ... if ... else [] }}"`), which
#: `ansible-inventory --host` hands over unrendered: the reader asks Ansible for their values.
TEMPLATED = ("bench_tests",)


def ci_policy(hv):
    """(run, destructive, why not) for a CI run of the board: what its `bench_ci` declares, and
    why it may not be run when it may not. A board with no `bench_ci` is never run by CI."""
    ci = hv.get("bench_ci")
    if ci is None:
        return False, False, "the layout does not mark it for CI (bench_ci: {run: true})"
    if not isinstance(ci, dict) or set(ci) - {"run", "destructive"} or \
            not all(isinstance(v, bool) for v in ci.values()):
        return False, False, f"bench_ci must be {{run: <bool>, destructive: <bool>}}, not {ci!r}"
    if not ci.get("run"):
        return False, False, "its bench_ci says run: false"
    return True, bool(ci.get("destructive")), ""


def presence(board):
    """(present, what devreg said) for a board on this host's bus: `resolve` exits 0 when it is
    there, 1 when it is absent, 2 when it matches more than one device."""
    r = subprocess.run([sys.executable, DEVREG, "resolve", board], capture_output=True, text=True, check=False,
                       env={**os.environ, "DEVREG_REGISTRY": site.registry_path()})
    said = (r.stdout.strip() or r.stderr.strip()).splitlines()
    return r.returncode, (said[0] if said else "")


def hostvars(layout_dir, board):
    """What the inventory publishes for one board of a layout, the templated declarations rendered."""
    # the pinned tools on PATH as for a play: a layout's SOPS vars need sops to be read at all
    inv = subprocess.run([os.path.join(VENV, "ansible-inventory"), "-i", layout_dir, "--host", board],
                         cwd=REPO, env=play_env({}), capture_output=True, text=True, check=False)
    if inv.returncode:
        sys.exit(f"exercise: {board}: {inv.stderr.strip() or inv.stdout.strip()}")
    return rendered(json.loads(inv.stdout), lambda key: debug_var(layout_dir, board, key))


def rendered(hv, fetch, keys=TEMPLATED):
    """`hv` with every value of `keys` that is still a template (a string holding `{{`) replaced by
    what `fetch(key)` renders it to."""
    out = dict(hv)
    for key in keys:
        if isinstance(out.get(key), str) and "{{" in out[key]:
            out[key] = fetch(key)
    return out


def debug_var(layout_dir, board, key):
    """One variable of one board as Ansible renders it (`ansible -m debug -a var=<key>`)."""
    r = subprocess.run([os.path.join(VENV, "ansible"), "-i", layout_dir, "-m", "debug", "-a", f"var={key}", board],
                       cwd=REPO, env=play_env({}), capture_output=True, text=True, check=False)
    return parse_debug(r.stdout, key)


def parse_debug(out, key):
    """The value of `key` in `debug`'s `host | SUCCESS => {...}` answer; None when it is not there."""
    at = out.find("=> ")
    if at < 0:
        return None
    try:
        return json.loads(out[at + 3:]).get(key)
    except ValueError:
        return None


def catalog_images():
    """The firmware catalog's images, as the `bench_plugins` lookup lists them."""
    from ansible_collections.rxrbench.core.plugins.module_utils import layout  # pylint: disable=import-outside-toplevel
    from ansible_collections.rxrbench.core.plugins.plugin_utils import registry  # pylint: disable=import-outside-toplevel
    return layout.load_catalog(*registry.catalog_dirs())["images"]


def all_images():
    return {**staged_images(os.path.join(REPO, ".build", "store")), **catalog_images()}


def family_accepts(hv):
    """`accept(fw)` for the board's family: whether its plugin takes the declaration for this
    board type (the layout's own rule, `Family.image`), or None when the plugins cannot be asked."""
    try:
        from ansible_collections.rxrbench.core.plugins.module_utils import layout  # pylint: disable=import-outside-toplevel
        from ansible_collections.rxrbench.core.plugins.plugin_utils import families, registry  # pylint: disable=import-outside-toplevel
        catalog = layout.load_catalog(*registry.catalog_dirs())
        fam = families.plugin(hv.get("bench_family"))
    except Exception:  # pylint: disable=broad-except  (no plugins here: fall back to the family rule)
        return None
    tag = hv.get("bench_board_tag")

    def accept(fw):
        try:
            rec, problems = fam.image(fw, catalog)
        except Exception:  # pylint: disable=broad-except
            return False
        return bool(rec) and not problems and (not tag or tag in (rec.get("board_tags") or []))
    return accept


# ---- running ------------------------------------------------------------------------------------

def play_env(spec, environ=None):
    """The environment a play runs in: the caller's, the pinned tools (sops, age) after the user's
    own as the recipes have it, colour off, and the state callbacks for a plan that records."""
    base = dict(os.environ if environ is None else environ)
    pins = os.path.join(base.get("HOME", os.path.expanduser("~")), ".local", "share", "rxrbench", "bin")
    path = base.get("PATH", "")
    if pins not in path.split(os.pathsep):
        base["PATH"] = (path + os.pathsep if path else "") + pins
    env = {**base, "ANSIBLE_FORCE_COLOR": "0", **(spec.get("env") or {})}
    if spec.get("state"):
        env["ANSIBLE_CALLBACKS_ENABLED"] = "rxrbench.core.state,rxrbench.core.timing"
    return env


def run_play(spec):
    """Run one play as `spec` says and return {rc, out, recap, state}: `recap` is the board's PLAY
    RECAP counts, `state` the state record the plan wrote (when `spec["state"]`). The output goes
    to `<run dir>/play.log`."""
    run_dir = os.path.join(spec["runs"], spec["run"])
    os.makedirs(run_dir, exist_ok=True)
    extra = {"bench_run": spec["run"], **(spec.get("extra") or {})}
    cmd = [os.path.join(VENV, "ansible-playbook"), "-i", spec["inventory"], os.path.join(PLAYBOOKS, spec["playbook"]),
           "-e", json.dumps(extra), *spec.get("args", [])]
    r = subprocess.run(cmd, cwd=REPO, env=play_env(spec), capture_output=True, text=True, check=False)
    out = r.stdout + r.stderr
    with open(os.path.join(run_dir, "play.log"), "a") as fh:
        fh.write("$ " + " ".join(cmd) + "\n" + out)
    recap = {m.group(1): {"ok": int(m.group(2)), "changed": int(m.group(3)), "unreachable": int(m.group(4)),
                          "failed": int(m.group(5)), "skipped": int(m.group(6))} for m in RECAP.finditer(out)}
    state = None
    if spec.get("state"):
        p = os.path.join(run_dir, "state.json")
        if os.path.isfile(p):
            with open(p) as fh:
                state = json.load(fh)
    return {"rc": r.returncode, "out": out, "recap": recap.get(spec["board"]) or {}, "state": state}


class Exercise:
    """One board of one layout through every verb that applies to it, in order, recorded."""

    def __init__(self, layout, board, destructive=False, skip=(), env=None, runs=RUNS, now=None,
                 plays=run_play, hv=None, images=None, layout_dir=None, hosts_dir=None, alts=None,
                 only=(), stale_only=False, entries=None, max_age=None, feats=None):
        self.layout, self.board, self.destructive = layout, board, destructive
        self.skip, self.env, self.runs, self.plays = set(skip), env or {}, runs, plays
        self.layout_dir = layout_dir or site.layout_path(layout)
        self.hosts_dir = hosts_dir
        self.hv = hv if hv is not None else hostvars(self.layout_dir, board)
        self.images = images if images is not None else all_images()
        self.accept = None if hv is not None else family_accepts(self.hv)
        self.alts = alts if alts is not None else alternatives(self.hv.get("bench_site_dir"), board)
        self.verbs = applicable(self.hv, self.images, self.accept, self.alts)
        self.run = now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.n = 0
        self.steps = []
        self.before = self.after = None
        self.halted = None          # why the writes after a failed one are skipped
        self.dirty = None           # why the writes are skipped when the start was not clean
        self.backups = {}           # this run's own backups by level ("regions", "chip"): their stamps
        # what to run of what applies: --only names verbs; --stale-only leaves out the verbs whose
        # last result (the ledger's `entries`) passed against the same code, board type and firmware
        self.only, self.stale_only, self.max_age = set(only), stale_only, max_age
        self.entries = entries or []
        self._feats = feats
        self.fresh = {}             # verb -> why it is left out as fresh
        self.ci = False             # a CI run (--ci): the board's bench_ci decided what runs

    # -- what each step depends on (tools/ledger.py) ------------------------------------------

    @property
    def feats(self):
        if self._feats is None:
            import features  # pylint: disable=import-outside-toplevel
            self._feats = features.load()
        return self._feats

    def board_tag(self):
        return self.hv.get("bench_board_tag")

    def firmware(self):
        import ledger  # pylint: disable=import-outside-toplevel
        fw = self.hv.get("firmware") if isinstance(self.hv.get("firmware"), dict) else {}
        return {**{k: fw.get(k) for k in ("family", "image", "class", "version", "build") if fw.get(k) is not None},
                "id": ledger.fw_id(self.hv)}

    def depends(self, verb):
        """(the features a verb exercises on this board, their fingerprint)."""
        import ledger  # pylint: disable=import-outside-toplevel
        if not hasattr(self, "_depends"):
            self._depends = {}  # pylint: disable=attribute-defined-outside-init
        if verb not in self._depends:
            ids = ledger.step_features(self.hv, verb, self.feats)
            self._depends[verb] = (ids, ledger.fingerprint(self.hv, ids, self.feats))
        return self._depends[verb]

    def choose(self):
        """Which applicable verbs this run leaves out (--only, --stale-only), each with why; the
        verbs a chosen one needs (its backup) are kept."""
        import ledger  # pylint: disable=import-outside-toplevel
        if not self.only and not self.stale_only:
            return
        fw = ledger.fw_id(self.hv)
        out = {}
        for v in self.verbs:
            name = v["verb"]
            if not v["applies"] or v["kind"] == NOT_RUN or name in ALWAYS:
                continue
            if self.only and name not in self.only:
                out[name] = "not among --only"
                continue
            if self.stale_only:
                _ids, fp = self.depends(name)
                ok, last = ledger.fresh(self.entries, self.board, name, self.board_tag(), fw, fp, self.max_age)
                if ok:
                    out[name] = (f"fresh: passed {last.get('ts')} at {last.get('tool_commit')}, and nothing it "
                                 "depends on has changed")
        will_run = [v["verb"] for v in self.verbs
                    if v["verb"] not in out and v["applies"] and v["kind"] != NOT_RUN and v["verb"] not in self.skip
                    and (v["kind"] != DESTRUCTIVE or self.destructive)]
        for name in will_run:
            for need in NEEDS.get(name, ()):
                out.pop(need, None)
        self.fresh = out

    # -- plays --------------------------------------------------------------------------------

    def _sub(self, verb):
        self.n += 1
        return f"{self.run}/exercise/{self.board}/{self.n:02d}-{verb}"

    def _play(self, verb, playbook, args=(), extra=None, state=False, inventory=None, limit=True):
        spec = {"playbook": playbook, "inventory": inventory or self.layout_dir, "board": self.board,
                "args": (["--limit", self.board] if limit else []) + list(args), "extra": extra or {},
                "env": self.env, "run": self._sub(verb), "runs": self.runs, "state": state}
        r = self.plays(spec)
        r["run_dir"] = os.path.join(self.runs, spec["run"])
        return r

    def _plan(self, verb, inventory=None):
        """A plan with the state record; (play result, the board's state record or {})."""
        r = self._play(verb, "apply.yml", ["--check", "--diff"], state=True, inventory=inventory)
        board = ((r.get("state") or {}).get("boards") or {}).get(self.board) or {}
        return r, board

    @staticmethod
    def _clean(board_state):
        return bool(board_state) and board_state.get("answered") is True and not board_state.get("failed") \
            and board_state.get("changes") == 0

    # -- the verbs ----------------------------------------------------------------------------

    def _hosts(self):
        return self.hosts_dir or site.layout_path("hosts")

    def v_survey(self, deep=False):
        host = self.hv.get("bench_access_host")
        if not host:
            return FAIL, {}, "the board has no bench host to survey"
        r = self._play("survey-deep" if deep else "survey", "survey.yml", ["--limit", host],
                       {"survey_deep": bool(deep)}, inventory=self._hosts(), limit=False)
        return (PASS if r["rc"] == 0 else FAIL), {"run_dir": r["run_dir"], "host": host}, ""

    def v_survey_deep(self):
        return self.v_survey(deep=True)

    @staticmethod
    def _failed(r):
        """Why a play on this board did not run as it should, or "": its exit code, and the board's
        own line in PLAY RECAP, which must be there (a play that matched no host exits 0)."""
        if r["rc"]:
            return f"the play failed (rc {r['rc']}): its log is in the run directory"
        recap = r.get("recap") or {}
        if not recap or not recap.get("ok"):
            return "the board is not in the play's recap: nothing ran on it"
        if recap.get("failed") or recap.get("unreachable"):
            return f"the play reports the board failed {recap.get('failed', 0)}, unreachable {recap.get('unreachable', 0)}"
        return ""

    def _rc_verb(self, verb, playbook, args=(), extra=None):
        r = self._play(verb, playbook, args, extra)
        why = self._failed(r)
        return (FAIL if why else PASS), {"run_dir": r["run_dir"], "recap": r["recap"]}, why

    def v_facts(self):
        return self._rc_verb("facts", "facts.yml")

    def v_plan(self):
        r, b = self._plan("plan")
        self.before = b
        ev = {"run_dir": r["run_dir"], "changes": b.get("changes"), "answered": b.get("answered"),
              "reported": b.get("reported"), "declared": b.get("declared")}
        if not b:
            self.dirty = "the plan recorded nothing for the board"
            return FAIL, ev, self.dirty
        if not self._clean(b):
            if b.get("failed"):
                self.dirty = "the plan failed on the board (its log is in the run directory)"
            elif not b.get("answered"):
                self.dirty = "the board did not answer its plan"
            else:
                self.dirty = f"the board did not match its layout at the start ({b.get('changes')} changes)"
            return FAIL, ev, self.dirty
        return PASS, ev, "clean"

    def v_secrets(self):
        return self._rc_verb("secrets", "secrets.yml")

    def v_diag(self):
        return self._rc_verb("diag", "diag.yml")

    def v_device_test(self):
        return self._rc_verb("device-test", "test.yml")

    def v_locate(self):
        return self._rc_verb("locate", "locate.yml", extra={"locate_seconds": 3})

    def v_boot(self):
        return self._rc_verb("boot", "boot.yml", ["--check", "--diff"])

    def v_reboot(self):
        return self._rc_verb("reboot", "reboot.yml")

    def _backup(self):
        """One backup play for both levels; run once, attributed to each level's verb."""
        if not hasattr(self, "_backup_result"):
            extra = {}
            if not any(v["applies"] for v in self.verbs if v["verb"] == "backup-chip"):
                extra["chip"] = False
            r = self._play("backup", "backup.yml", extra=extra)
            if not self._failed(r):
                self.backups = {level: stamp for level, stamp in BACKUP_STAMP.findall(r.get("out") or "")}
            self._backup_result = r  # pylint: disable=attribute-defined-outside-init
        return self._backup_result

    def _backup_verb(self, level):
        r = self._backup()
        ev = {"run_dir": r["run_dir"], "recap": r["recap"], "backup": self.backups.get(level)}
        if self._failed(r):
            return FAIL, ev, "the backup failed: " + self._failed(r)
        if not self.backups.get(level):
            return FAIL, ev, f"the backup play named no {level} backup"
        return PASS, ev, ""

    def v_backup_regions(self):
        return self._backup_verb("regions")

    def v_backup_chip(self):
        return self._backup_verb("chip")

    def _ours(self, level):
        """This run's own backup at `level` (its stamp), or None: a restore writes back only what
        this run read, never an older backup, and the erase it undoes is not run without one."""
        return self.backups.get(level)

    def v_keys_export(self):
        return self._rc_verb("keys-export", "keys-export.yml")

    def v_apply(self):
        r = self._play("apply", "apply.yml", ["--diff"])
        changed = r["recap"].get("changed")
        ev = {"run_dir": r["run_dir"], "recap": r["recap"]}
        if self._failed(r):
            return FAIL, ev, "the apply failed: " + self._failed(r)
        if changed:
            return FAIL, ev, f"expected nothing to change; {changed} tasks changed"
        return PASS, ev, ""

    def v_restore_regions(self):
        stamp = self._ours("regions")
        if not stamp:
            return SKIP, {}, "no region backup from this run to write back"
        r = self._play("restore-regions", "restore-regions.yml", extra={"restore_from": stamp})
        if r["rc"]:
            return FAIL, {"run_dir": r["run_dir"]}, "the restore failed"
        _, b = self._plan("plan-after-restore-regions")
        return (PASS if self._clean(b) else FAIL), {"run_dir": r["run_dir"], "plan_after": b.get("changes")}, \
            "" if self._clean(b) else "the plan after the restore is not clean"

    def v_snapshot_switch(self):
        name = f"ex-{self.run}"
        s = self._play("snapshot", "snapshot.yml", extra={"snapshot_name": name})
        if s["rc"]:
            return FAIL, {"run_dir": s["run_dir"]}, "the snapshot failed"
        w = self._play("switch", "switch.yml", extra={"snapshot_name": name})
        ev = {"snapshot": name, "run_dir": w["run_dir"], "switch_recap": w["recap"]}
        if w["rc"]:
            return FAIL, ev, "the switch back failed"
        _, b = self._plan("plan-after-switch")
        ev["plan_after"] = b.get("changes")
        return (PASS if self._clean(b) else FAIL), ev, "" if self._clean(b) else "the plan after the switch is not clean"

    def v_update_there_and_back(self):
        second = other_image(self.hv, self.images, self.accept)
        if not second:
            return SKIP, {}, "no second image"
        import campaign  # pylint: disable=import-outside-toplevel
        where = os.path.join(self.runs, self.run, "exercise", self.board, "state-b")
        os.makedirs(where, exist_ok=True)
        mine = campaign.make_state(self.layout_dir, {"boards": {self.board: {"firmware": second}}}, where)
        ev = {"there": second, "state_dir": mine}
        a = self._play("update-there", "apply.yml", ["--diff"], inventory=mine)
        if a["rc"]:
            # an apply that failed may have written part of the image: come back regardless
            ev["run_dir"] = a["run_dir"]
            return FAIL, ev, self._update_back(ev) or "the update there failed (came back)"
        _, b = self._plan("plan-there", inventory=mine)
        if not self._clean(b):
            ev["plan_there"] = b.get("changes")
            return FAIL, ev, self._update_back(ev) or "the plan on the second image is not clean (came back)"
        ev["there_reported"] = b.get("reported")
        problem = self._update_back(ev)
        if problem:
            return FAIL, ev, problem
        _, b2 = self._plan("plan-back")
        ev["plan_back"] = b2.get("changes")
        return (PASS if self._clean(b2) else FAIL), ev, "" if self._clean(b2) else "the plan after coming back is not clean"

    def _update_back(self, ev):
        """The layout's own image again: by apply, or by restore-chip from this run's backup when
        the apply fails. None when it came back, else what went wrong."""
        back = self._play("update-back", "apply.yml", ["--diff"])
        ev["back_by"] = "apply"
        if not back["rc"]:
            return None
        ev["run_dir"] = back["run_dir"]
        stamp = self._ours("chip")
        if not stamp:
            return "the update back failed, and this run has no chip backup to restore"
        r = self._play("restore-chip-after-update", "restore-chip.yml", extra={"restore_from": stamp})
        ev["back_by"] = "restore-chip"
        return "the update back failed; restore-chip " + ("failed too" if r["rc"] else "brought the image back")

    def v_keys_restore(self):
        return self._rc_verb("keys-restore", "keys-restore.yml", ["--check", "--diff"])

    def v_erase_region(self):
        region = (_config_regions(self.hv) or [None])[0]
        if not self._ours("regions"):
            return SKIP, {"region": region}, "no region backup from this run: the region is not erased"
        e = self._play("erase-region", "erase.yml", extra={"erase_region": region})
        ev = {"region": region, "run_dir": e["run_dir"]}
        if e["rc"]:
            return FAIL, ev, "the erase failed"
        a = self._play("apply-after-erase", "apply.yml", ["--diff"])
        if a["rc"]:
            return FAIL, ev, "the apply after the erase failed"
        if _device_secret(self.hv):
            k = self._play("keys-restore-after-erase", "keys-restore.yml")
            if k["rc"]:
                return FAIL, ev, "keys-restore after the erase failed"
        _, b = self._plan("plan-after-erase")
        ev["plan_after"] = b.get("changes")
        if not self._clean(b):
            return FAIL, ev, "the plan after the erase and restore is not clean"
        if self.hv.get("bench_secret_kinds"):
            s = self._play("secrets-after-erase", "secrets.yml")
            if s["rc"]:
                return FAIL, ev, "the secrets check after the erase failed"
        return PASS, ev, ""

    def v_erase_chip(self):
        stamp = self._ours("chip")
        if not stamp:
            return SKIP, {}, "no chip backup from this run: the chip is not erased"
        e = self._play("erase-chip", "erase-chip.yml")
        ev = {"run_dir": e["run_dir"], "backup": stamp}
        if e["rc"]:
            return FAIL, ev, "the chip erase failed"
        r = self._play("restore-chip", "restore-chip.yml", extra={"restore_from": stamp})
        if r["rc"]:
            return FAIL, ev, "the chip restore failed"
        _, b = self._plan("plan-after-restore-chip")
        ev["plan_after"] = b.get("changes")
        return (PASS if self._clean(b) else FAIL), ev, "" if self._clean(b) else "the plan after the chip restore is not clean"

    def v_family_switch(self):
        """The board applied as the site's alternative declares it (another family, through a
        state copy of the layout with that declaration), planned clean there; then back as the
        layout declares it, by apply, or by restore-chip from this run's backup when the apply
        back fails; the escrowed key given back where the device makes one; a clean plan; the
        secrets check. The bench host is set up for each declaration first (the brokers)."""
        alt = other_family(self.hv, self.images, self.alts)
        if not alt:
            return SKIP, {}, "no alternative declares another family"
        if _has_chip(self.hv) and not self._ours("chip"):
            return SKIP, {}, "no chip backup from this run, the way back if the apply back fails"
        import campaign  # pylint: disable=import-outside-toplevel
        where = os.path.join(self.runs, self.run, "exercise", self.board, "state-alt")
        os.makedirs(where, exist_ok=True)
        mine = campaign.make_state(self.layout_dir, {"boards": {self.board: alt["vars"]}}, where)
        ev = {"alternative": alt["name"], "family": alt["vars"]["firmware"].get("family"), "state_dir": mine}
        # the bench host as the other declaration needs it: a family with a console is written
        # through a serial broker, which only the host play starts (and retires on the way back)
        h = self._hosts_play("hosts-there", inventory=mine)
        if h["rc"]:
            return self._switch_back(ev, "the bench host could not be set up for the other family")
        a = self._play("switch-there", "apply.yml", ["--diff"], inventory=mine)
        if a["rc"]:
            return self._switch_back(ev, "the switch to the other family failed")
        _, b = self._plan("plan-switched", inventory=mine)
        ev["plan_there"], ev["there_reported"] = b.get("changes"), b.get("reported")
        return self._switch_back(ev, "" if self._clean(b) else "the plan on the other family is not clean")

    def _hosts_play(self, verb, inventory=None):
        """The layout's (or a state copy's) host play on this board's bench host: devreg, the
        registry copy, the brokers as that declaration wants them."""
        host = self.hv.get("bench_access_host")
        return self._play(verb, "host.yml", ["--limit", host] if host else [], inventory=inventory, limit=False)

    def _switch_back(self, ev, problem):
        h = self._hosts_play("hosts-back")
        if h["rc"]:
            return FAIL, ev, problem or "the bench host could not be set up again for the layout"
        back = self._play("switch-back", "apply.yml", ["--diff"])
        ev["back_by"] = "apply"
        if back["rc"] and _has_chip(self.hv) and self._ours("chip"):
            r = self._play("restore-chip-after-switch", "restore-chip.yml", extra={"restore_from": self._ours("chip")})
            ev["back_by"] = "restore-chip"
            if r["rc"]:
                return FAIL, ev, problem or "the way back failed: apply, then restore-chip"
        elif back["rc"]:
            return FAIL, ev, problem or "the apply back failed"
        if _device_secret(self.hv):
            k = self._play("keys-restore-after-switch", "keys-restore.yml")
            if k["rc"]:
                return FAIL, ev, problem or "keys-restore after the switch back failed"
        _, b = self._plan("plan-switched-back")
        ev["plan_back"] = b.get("changes")
        if not self._clean(b):
            return FAIL, ev, problem or "the plan after the switch back is not clean"
        if self.hv.get("bench_secret_kinds"):
            s = self._play("secrets-after-switch", "secrets.yml")
            if s["rc"]:
                return FAIL, ev, problem or "the secrets check after the switch back failed"
        return (FAIL if problem else PASS), ev, problem

    def _came_back(self):
        """(came_back, result, msg). A board that did not match its layout at the start had every
        write skipped: whether it came back was not tested (None) when it ends as it started, and
        is False when it does not."""
        after, before = self.after or {}, self.before or {}
        if self.dirty:
            def seen(b):
                return b.get("answered"), b.get("changes"), b.get("reported")
            if seen(after) == seen(before):
                return None, SKIP, f"not tested: {self.dirty}; the board ends as it started"
            return False, FAIL, f"{self.dirty}, and the board does not end as it started"
        ok = self._clean(after) and (not before or after.get("reported") == before.get("reported"))
        return ok, (PASS if ok else FAIL), "" if ok else "the board did not come back as it was"

    # -- the run ------------------------------------------------------------------------------

    def _record_step(self, v, result, evidence, msg, seconds):
        step = {**v, "result": result, "seconds": round(seconds, 1), "evidence": evidence, "msg": msg}
        if v.get("applies"):
            step["features"], step["fp"] = self.depends(v["verb"])
        self.steps.append(step)

    def _why_skipped(self, v):
        if v["verb"] in self.skip:
            return "skipped on request"
        if v["verb"] in self.fresh:
            return self.fresh[v["verb"]]
        if not v["applies"]:
            return v["reason"]
        if v["kind"] == NOT_RUN:
            return v["reason"]
        if v["kind"] in (REVERSIBLE, DESTRUCTIVE) and v["verb"] not in HOST_ONLY:
            if self.dirty:
                return "writes skipped: " + self.dirty
            if self.halted:
                return "writes skipped after a failed one: " + self.halted
        if v["kind"] == DESTRUCTIVE and not self.destructive:
            return "destructive verbs run only with --destructive"
        return None

    def go(self):
        """Run every step and return the record."""
        start = time.monotonic()
        self.choose()
        for v in self.verbs:
            why = self._why_skipped(v)
            if why:
                self._record_step(v, SKIP, {}, why, 0)
                continue
            fn = getattr(self, "v_" + v["verb"].replace("-", "_"))
            t = time.monotonic()
            try:
                result, evidence, msg = fn()
            except Exception as e:  # pylint: disable=broad-except  (one verb's error must not lose the record)
                result, evidence, msg = FAIL, {}, f"{type(e).__name__}: {e}"
            self._record_step(v, result, evidence, msg, time.monotonic() - t)
            if result == FAIL and v["kind"] in (REVERSIBLE, DESTRUCTIVE) and not self.halted:
                self.halted = f"{v['verb']}: {msg or 'failed'}"
        t = time.monotonic()
        r, self.after = self._plan("plan-end")
        came_back, result, msg = self._came_back()
        self._record_step({"verb": "came-back", "kind": READ, "applies": True,
                           "reason": "the final plan is clean and the board reports what it reported at the start"},
                          result,
                          {"run_dir": r["run_dir"], "before": (self.before or {}).get("reported"),
                           "after": (self.after or {}).get("reported"), "changes": (self.after or {}).get("changes")},
                          msg, time.monotonic() - t)
        rec = {
            "v": 2, "run": self.run, "layout": self.layout, "board": self.board, "destructive": self.destructive,
            "ci": self.ci,
            "board_tag": self.board_tag(), "firmware": self.firmware(),
            "selected": {"only": sorted(self.only), "stale_only": self.stale_only, "left_out": sorted(self.fresh)},
            "family": self.hv.get("bench_family"), "flasher": self.hv.get("bench_flasher"),
            "routes": sorted(_bearers(self.hv) - {None}), "tool_commit": _commit(REPO),
            "site_commit": _commit(self.hv.get("bench_site_dir")),
            "seconds": round(time.monotonic() - start, 1), "came_back": came_back,
            "passed": all(s["result"] != FAIL for s in self.steps), "steps": self.steps,
        }
        path = os.path.join(self.runs, self.run, "exercise", f"{self.board}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(rec, fh, indent=1)
        rec["path"] = path
        return rec


def keep(rec, site_dir=None):
    """Copy a record to the site as the board's latest (`<site>/exercise/<board>.json`) and append its
    steps to the site's ledger (tools/ledger.py); the record's path."""
    import ledger  # pylint: disable=import-outside-toplevel
    site_dir = site_dir or site.site_dir()
    path = os.path.join(site_dir, "exercise", f"{rec['board']}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({k: v for k, v in rec.items() if k not in ("path", "kept")}, fh, indent=1)
    ledger.append(ledger.path_of(site_dir), ledger.lines_of(rec))
    return path


def _commit(repo):
    if not repo or not os.path.isdir(repo):
        return None
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False)
    return r.stdout.strip() or None


def table(rows, key="result"):
    width = max(len(r["verb"]) for r in rows)
    return "\n".join(f"{r['kind']:<11} {r[key]:<8} {r['verb']:<{width}}  {r.get('msg') or r['reason']}" for r in rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("layout")
    ap.add_argument("board")
    ap.add_argument("confirm", nargs="?", default="",
                    help="confirm=apply runs the exercise, which writes to the board; without it, what would run")
    ap.add_argument("--destructive", action="store_true", help="run the destructive verbs too (each with its restore)")
    ap.add_argument("--skip", default="", help="verbs to skip, comma-separated")
    ap.add_argument("--list", action="store_true", help="say what applies and stop")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record", action="store_true",
                    help="keep the record as the site's latest for the board (<site>/exercise/<board>.json) "
                         "and its steps in the site's ledger (exercise/ledger.jsonl)")
    ap.add_argument("--only", default="", help="run only these verbs (comma-separated), with what they need")
    ap.add_argument("--stale-only", action="store_true",
                    help="leave out every verb that passed last time against the same code, board type and "
                         "firmware (the site's ledger); the start plan and came-back always run")
    ap.add_argument("--ci", action="store_true",
                    help="a CI run: only a board marked bench_ci: {run: true}; destructive verbs only "
                         "where it says destructive: true")
    ap.add_argument("--max-age", type=int, default=None, metavar="DAYS",
                    help="with --stale-only: a pass older than this is stale too")
    a = ap.parse_args(argv)
    if a.confirm not in ("", "confirm=apply"):
        ap.error(f"unknown argument {a.confirm!r}: the one word taken here is confirm=apply")
    if a.ci and a.destructive:
        ap.error("--ci takes the destructive verbs from the board's bench_ci, not from --destructive")
    site.load_env()
    entries = None
    if a.stale_only:
        import ledger  # pylint: disable=import-outside-toplevel
        entries = ledger.read(ledger.path_of(site.site_dir()))
    ex = Exercise(a.layout, a.board, destructive=a.destructive, skip=[s for s in a.skip.split(",") if s],
                  only=[s for s in a.only.split(",") if s], stale_only=a.stale_only, entries=entries,
                  max_age=a.max_age)
    if a.ci:
        run, destructive, why = ci_policy(ex.hv)
        if not run:
            print(f"exercise {a.board}: not run by CI: {why}")
            return 2
        if a.confirm == "confirm=apply" and not a.list:
            rc, said = presence(a.board)
            if rc == 1:
                print(f"exercise {a.board}: not present on this host ({said}): not run, nothing recorded")
                return NOT_PRESENT
            if rc:
                print(f"exercise {a.board}: devreg cannot resolve it ({said}): not run")
                return 1
        ex.ci, ex.destructive = True, destructive
    if a.list or a.confirm != "confirm=apply":
        ex.choose()
        # The exercise writes to the board (an apply, a region restore, a snapshot and the switch
        # back, an image update and back; with --destructive, erases): like every recipe that
        # writes, it runs only with confirm=apply, and says what it would run otherwise.
        rows = [{**v, "result": ("left out" if v["verb"] in ex.fresh else "applies") if v["applies"] else "no",
                 "msg": ex.fresh.get(v["verb"]) or ""} for v in ex.verbs]
        print(json.dumps(ex.verbs, indent=1) if a.json else table(rows))
        if not a.list and not a.json:
            print(f"exercise {a.board}: a plan, nothing was run; add confirm=apply to run it (it writes to the board)")
        return 0
    rec = ex.go()
    if a.record:
        rec["kept"] = keep(rec)
    if a.json:
        print(json.dumps(rec, indent=1))
    else:
        print(table(rec["steps"]))
        print(f"exercise {a.board}: {'passed' if rec['passed'] else 'FAILED'}, came back: {'not tested' if rec['came_back'] is None else rec['came_back']}, "
              f"{rec['seconds']} s, record {rec['path']}" + (f", kept as {rec['kept']}" if a.record else ""))
    return 0 if rec["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
