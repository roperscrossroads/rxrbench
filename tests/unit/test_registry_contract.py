"""The registry contract (docs/REGISTRY.md) is what the code reads, no more.

A ratchet on the string keys the tool's registry readers subscript or `.get()`: every key a
reader uses must be in the record (`tests/fixtures/registry-keys.txt`), and a key in the record
must be one the contract page names. A new read of a registry key is then a deliberate act:
name it in docs/REGISTRY.md and re-record with RATCHET_RECORD=1. The record only grows by that
route, so a site's own keys (its notes, its measurements) can never become a requirement
by accident. The example registry and the fixture together must exercise every required key
and every bearer the page names.
"""
import ast
import os
import re

import yaml

from tests.support.requirement import breach

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE = os.path.join("collections", "ansible_collections", "rxrbench", "core")
#: Every file that reads the registry (docs/REGISTRY.md "what the tool reads").
READERS = (
    "devreg/devreg.py",
    f"{CORE}/plugins/module_utils/layout.py",
    f"{CORE}/plugins/module_utils/routes.py",
    f"{CORE}/plugins/module_utils/location.py",
    f"{CORE}/plugins/module_utils/hwsurvey.py",
    f"{CORE}/plugins/inventory/devreg.py",
    f"{CORE}/host/bin/uf2_flash.py",
    f"{CORE}/plugins/modules/card_write.py",
    f"{CORE}/plugins/module_utils/flash/card.py",
    f"{CORE}/plugins/modules/usb_survey.py",
    "tools/enroll.py",
    "tools/usbipd_attach.py",
    "tools/plug_relay.py",
    "tools/labels.py",
    "tools/debug.py",
)
RECORD = os.path.join(REPO, "tests", "fixtures", "registry-keys.txt")
DOC = os.path.join(REPO, "docs", "REGISTRY.md")
#: What a registry must have for the inventory and devreg to work at all.
REQUIRED = {"devices", "hosts", "kind", "match", "reach", "policy", "board_tag", "ssh", "can",
            "state", "host", "usb_serial", "serial_by_id", "usb_vid", "usb_pid", "aliases"}


#: What a reader holds a board's host variables in (layout.py: the layout's own `all_hv[name]`,
#: and the variables it computes for the inventory, `<board>["vars"]`). A key read from these is
#: a layout variable, not a registry key: it belongs to docs/LAYOUT.md, not this contract.
HOST_VAR_ROOTS = {"all_hv", "hv", "hostvars"}


def _host_vars(node):
    """Whether `node` (what a key is read from) holds host variables: rooted at one of
    HOST_VAR_ROOTS, or a `["vars"]` subscript on the way."""
    while True:
        if isinstance(node, ast.Name):
            return node.id in HOST_VAR_ROOTS
        if isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Constant) and node.slice.value == "vars":
                return True
            node = node.value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            node = node.func.value
        else:
            return False


def keys_in(source):
    """The string keys `source` reads by subscript or `.get()`: a superset of its registry reads
    (sysfs and route records share names with registry keys, which is what the record absorbs).
    Not counted: a key assigned (a subscript stored to) and a key read from host variables
    (_host_vars), which are layout variables."""
    out = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) \
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str) \
                and not _host_vars(node):
            out.add(node.slice.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) \
                and not _host_vars(node.func.value):
            out.add(node.args[0].value)
    return {k for k in out if re.fullmatch(r"[a-z][a-z0-9_]*", k)}


def keys_read(path):
    return keys_in(open(os.path.join(REPO, path), encoding="utf-8").read())


def test_host_variable_reads_and_assignments_are_not_registry_reads():
    """The check's own case (docs/ARCHITECTURE.md#the-tools-own-tests): layout.py reads registry entries and host
    variables side by side; only the registry reads count."""
    src = ("x = entry.get('kind')\n"
           "y = (entry.get('policy') or {}).get('board_tag')\n"
           "z = entry['match']\n"
           "a = all_hv[name].get('bench_check_depth', 'full')\n"
           "c = all_hv[name]['bench_foreign']\n"
           "d = b['vars'].get('bench_flasher')\n"
           "e = b['vars']['bench_via']\n"
           "b['vars']['bench_family'] = fam\n")
    assert keys_in(src) == {"kind", "policy", "board_tag", "match"}


def documented():
    """Every key docs/REGISTRY.md names in backticks, split on `,` and `.` and `[]`."""
    text = open(DOC, encoding="utf-8").read()
    out = set()
    for tick in re.findall(r"`([^`]+)`", text):
        for part in re.split(r"[,.\[\]<>{}/| ]+", tick):
            if re.fullmatch(r"[a-z][a-z0-9_]*", part):
                out.add(part)
    return out


def recorded():
    out = {}
    with open(RECORD, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            path, keys = line.split(":", 1)
            out[path.strip()] = set(keys.split())
    return out


def test_every_registry_key_a_reader_uses_is_recorded_and_documented():
    doc = documented()
    now = {p: keys_read(p) for p in READERS}
    if os.environ.get("RATCHET_RECORD"):
        with open(RECORD, "w", encoding="utf-8") as fh:
            fh.write("# The string keys each registry reader subscripts or .get()s (test_registry_contract.py).\n"
                     "# Re-record with RATCHET_RECORD=1 after naming a new registry key in docs/REGISTRY.md.\n")
            for p in READERS:
                fh.write(f"{p}: {' '.join(sorted(now[p]))}\n")
    rec = recorded()
    new = {p: sorted(now[p] - rec.get(p, set())) for p in READERS if now[p] - rec.get(p, set())}
    assert not new, breach(
        [f"{p}: {' '.join(k)}" for p, k in new.items()],
        "the registry contract (docs/REGISTRY.md) is what the code reads: a site's own keys never "
        "become a requirement by accident",
        "a new read of a registry key: name the key in docs/REGISTRY.md, then re-record "
        "(RATCHET_RECORD=1)",
        "a key that is not a registry key (a sysfs or route field the reader also subscripts): "
        "re-record (RATCHET_RECORD=1); the record holds both, the contract page only registry keys")
    # a recorded key that is a registry key must be documented; the record also holds the
    # readers' own record fields (sysfs, routes), which are not registry keys: those are the
    # ones the contract page does not name, listed here so a new one is seen
    gone = {p: sorted(rec.get(p, set()) - now[p]) for p in READERS if rec.get(p, set()) - now[p]}
    assert not gone, f"recorded keys no longer read (re-record): {gone}"
    contract = _contract_keys()
    undocumented = sorted(contract - doc)
    assert not undocumented, f"CONTRACT keys devreg knows but docs/REGISTRY.md does not name: {undocumented}"
    assert REQUIRED <= doc, f"required keys missing from docs/REGISTRY.md: {sorted(REQUIRED - doc)}"


def _contract_keys():
    import importlib.util
    spec = importlib.util.spec_from_file_location("devreg_contract", os.path.join(REPO, "devreg", "devreg.py"))
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.path.insert(0, os.path.join(REPO, "devreg"))
    os.environ.setdefault("DEVREG_REGISTRY", os.path.join(REPO, "tests", "fixtures", "registry.yaml"))
    spec.loader.exec_module(mod)
    keys = set()
    for block in mod.CONTRACT.values():
        keys |= set(block)
    return keys | set(mod.BEARERS)


def test_the_example_and_the_fixture_exercise_the_contract():
    example = yaml.safe_load(open(os.path.join(REPO, "site.example", "registry.yaml")))
    fixture = yaml.safe_load(open(os.path.join(REPO, "tests", "fixtures", "registry.yaml")))
    seen = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                seen.add(str(k))
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(example)
    walk(fixture)
    missing = sorted(REQUIRED - seen)
    assert not missing, f"the example and fixture registries never use: {missing}"
    bearers = set()
    for reg in (example, fixture):
        for d in (reg.get("devices") or {}).values():
            bearers |= set((d or {}).get("reach") or {})
    assert {"usb", "ssh", "ble", "swd"} <= bearers, f"bearers the fixtures never use: {sorted({'usb', 'ssh', 'ble', 'swd'} - bearers)}"
