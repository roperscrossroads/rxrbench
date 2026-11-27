import os
import re
import subprocess
import sys

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
from ansible_collections.rxrbench.core.plugins.plugin_utils import families
from ansible_collections.rxrbench.core.plugins.plugin_utils.families import base
from tests.support.requirement import breach

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MU = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "plugins", "module_utils")
# where the family plugins live: core's own, and each bundled plugin collection's
FAMILY_DIRS = [os.path.join(REPO, "collections", "ansible_collections", "rxrbench", c, "plugins",
                            "plugin_utils", "families") for c in ("core", "mesh", "linux", "zephyr", "nuttx", "launcher")]
FIELDS = {"vocabulary", "state_command", "identity_key", "build", "config_via", "radio", "update",
          "console", "locate", "plugin"}


def test_every_family_row_has_every_field():
    for name, row in {**families.FAMILIES, "<foreign>": families.FOREIGN}.items():
        assert set(row) == FIELDS, name


def test_an_unknown_family_is_foreign_and_unmanaged():
    assert families.of("no-such-firmware") is families.FOREIGN
    assert families.radio("no-such-firmware") == "unmanaged"
    assert families.plugin("no-such-firmware") is families.PLUGINS["foreign"]


@pytest.mark.plugins
def test_every_row_has_a_plugin_and_every_plugin_a_row():
    rows = {**families.FAMILIES, "<foreign>": families.FOREIGN}
    for name, row in rows.items():
        assert row["plugin"] in families.PLUGINS, name
        assert isinstance(families.plugin(name), base.Family)
    modules = {f[:-3] for d in FAMILY_DIRS for f in os.listdir(d)
               if f.endswith(".py") and f not in ("__init__.py", "base.py")}
    assert modules == set(families.PLUGINS) == {r["plugin"] for r in rows.values()}


def test_the_package_imports_on_its_own():
    """Modules run on a bench host from what Ansible saw imported; a plugin that pulled in
    layout, rfrules or flash would be a cycle here and a missing module there."""
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, 'collections'); "
                        "from ansible_collections.rxrbench.core.plugins.plugin_utils import families; "
                        "print(sorted(families.PLUGINS))"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for name in ("layout", "rfrules", "flash"):
        assert f"module_utils.{name}" not in r.stderr


def _walk(tops, suffixes=(".py", ".yml", ".yaml")):
    for top in tops:
        for root, _dirs, files in os.walk(os.path.join(REPO, top)):
            for f in files:
                if f.endswith(suffixes):
                    yield os.path.join(root, f)


# Comparing a family, plugin or board type by name is test_portability.py's (an AST check).


# Generic code must not read one family's block or name its groups. Until the family plugin
# owns all of it, this is a ratchet: each file may carry at most the hits it has today, and a
# step that moves a concern behind a hook lowers its number. A path not listed may carry none.
FLEET_SHAPE = re.compile(r'''"zephyrtastic"|\.get\("zephyrtastic"\)|zephyrtastic\.(intents|brain|head|listener|led|cluster)'''
                         r"""|'heads'|'listeners'|'brains'|cluster_c|builds\.zephyrtastic""")
FAMILY_OWNED = ("collections/ansible_collections/rxrbench/core/plugins/plugin_utils/families/",
                "collections/ansible_collections/rxrbench/mesh/")
RATCHET = {}


def test_generic_code_does_not_know_one_familys_shape():
    over, counts = [], {}
    for path in _walk(("collections", "tools")):
        rel = os.path.relpath(path, REPO)
        if rel.startswith(FAMILY_OWNED):
            continue
        n = sum(1 for line in open(path, encoding="utf-8", errors="replace") if FLEET_SHAPE.search(line))
        if n:
            counts[rel] = n
        if n > RATCHET.get(rel, 0):
            over.append(f"{rel}: {n} (allowed {RATCHET.get(rel, 0)})")
    assert over == [], breach(["one family's shape in generic code:"] + over,
                              "core-portability P2 (docs/ARCHITECTURE.md#core-portability)",
                              "move the read of the family's block behind a Family hook in its plugin")
    slack = {p: a for p, a in RATCHET.items() if counts.get(p, 0) < a}
    assert slack == {}, f"lower the ratchet: {slack}"


@pytest.mark.plugins
def test_a_family_refuses_an_image_that_is_not_what_the_layout_declares():
    refuse = familyspec.image_refusals
    assert refuse("zephyrtastic", {"class": 4, "features": []}, {"class": 4}) == []
    assert "class 6" in refuse("zephyrtastic", {"class": 6}, {"class": 4})[0]
    assert "['hub']" in refuse("zephyrtastic", {"class": 4, "features": ["hub"]}, {"class": 4})[0]
    # a feature's former name is the feature, in the image and in the layout
    assert refuse("zephyrtastic", {"class": 4, "features": ["brain"]},
                  {"class": 4, "features": ["hub"]}) == []
    assert refuse("zephyrtastic", {"class": 4, "features": ["hub"]},
                  {"class": 4, "features": ["brain"]}) == []
    assert refuse("zephcore", {"role": "repeater"}, {"role": "repeater"}) == []
    assert "role 'companion'" in refuse("zephcore", {"role": "companion"}, {"role": "repeater"})[0]
    assert refuse("bitpirate", {"class": 1}, {"image": "x"}) == []



def test_an_image_is_refused_unless_it_is_what_the_layout_declares_by_the_familys_match(widgets):
    """Core's rule, on the fixture plugin's widget (image_match: class and features)."""
    refuse = familyspec.image_refusals
    assert refuse("widget", {"class": 4, "features": []}, {"class": 4}) == []
    assert "class 6" in refuse("widget", {"class": 6}, {"class": 4})[0]
    assert "['fast']" in refuse("widget", {"class": 4, "features": ["fast"]}, {"class": 4})[0]
    assert refuse("widget", {"class": 4, "features": ["fast"]}, {"class": 4, "features": ["fast"]}) == []

def test_every_family_resolves_to_data_a_bench_host_can_use():
    """A module on a bench host cannot import a plugin: the spec is all it gets, so it must
    survive the trip (plain JSON) and say how every family with a state line is read, or name
    the probe a family with none is read by (one its plugin ships, for its bench hosts), or
    the console command whose prose answer says it."""
    import json
    import os
    specs = families.specs()
    assert json.loads(json.dumps(specs)) == specs
    for name, row in families.FAMILIES.items():
        ident = specs[name]["identity"]
        if row["state_command"]:
            assert ident and "command" in ident, name
        elif ident and ident.get("lineless"):
            # read in prose on its console: the fallback alone says the identity
            assert ident.get("command") and ident["fallback"]["fields"], name
            assert familyspec.state_command(specs[name]) is None, name
        elif ident:
            # ansible_collections.<ns>.<plugin>.plugins.plugin_utils.families.<name>
            collection = type(families.plugin(name)).__module__.split(".")[:3]
            assert ident.get("tool") and ident.get("fields"), name
            shipped = os.path.join(REPO, "collections", *collection, "host", "bin", ident["tool"])
            assert os.path.exists(shipped), (name, shipped)
    assert familyspec.of("no-such-firmware") == familyspec.GENERIC


@pytest.mark.plugins
def test_a_family_describes_and_tags_what_the_survey_found():
    zt = families.plugin("zephyrtastic")
    boards = {"xiao": {"mcu": "nrf52840", "zephyr_boards": ["xiao_ble"]}}
    # a worker, as its state line says it now (worker) and before the rename (head)
    for key in ("worker", "head"):
        probe = {"node_id": "0x0a0b0c0d", "board": "xiao_ble", "class": 12, "build": "abc", key: True}
        assert {"fw:zephyrtastic", "mcu:nrf52840", "class:12", "board:xiao_ble", "role:worker"} \
            <= zt.survey_tags(probe, boards)
        assert zt.survey_describe(probe) == \
            "zephyrtastic class 12 on xiao_ble, build abc, node 0x0a0b0c0d, worker"
    st = families.plugin("meshtastic")
    assert "fw:stock-meshtastic" in st.survey_tags({"hw_model": "X", "has_bluetooth": True})
    assert st.survey_describe({}) == ""
    hv = zt.enroll_hostvars("kit", {"family": "zephyrtastic", "class": 4}, {})
    assert hv["firmware"] == {"family": "zephyrtastic", "class": 4} and hv["meshtastic"]["owner"] == "kit"


# A module on a bench host is shipped the module_utils it imports, and a plugin collection's
# code is not among them: what runs there takes the families as data (familyspec.py). So
# nothing in core's modules or module_utils may import plugin_utils, but the two that only
# the controller uses (the inventory plugin and the tools call them).
CONTROLLER_ONLY = {"layout.py", "rflayout.py"}


def test_code_that_runs_on_a_bench_host_does_not_import_the_families():
    core = os.path.dirname(MU)
    found = set()
    for sub in ("modules", "module_utils"):
        for path in _walk((os.path.join(core, sub),), (".py",)):
            if "plugin_utils" in open(path, encoding="utf-8").read():
                found.add(os.path.relpath(path, core))
    want = {os.path.join("module_utils", f) for f in CONTROLLER_ONLY}
    assert found == want, breach(
        [f"imports plugin_utils on a bench host: {sorted(found - want)}"] if found - want
        else [f"no longer imports plugin_utils (drop from CONTROLLER_ONLY): {sorted(want - found)}"],
        "a module on a bench host runs without the plugins: it takes the families as data "
        "(bench_families, familyspec.py), docs/PLUGINS.md",
        "read what the module needs from bench_families (Family.spec) instead of importing",
        "a file only the controller runs belongs in CONTROLLER_ONLY, with why in its comment")


@pytest.mark.plugins
def test_zephyrtastic_counters_judge_the_errors_of_its_state_line_and_keep_the_rest():
    from ansible_collections.rxrbench.core.plugins.filter.counters import counter_delta
    spec = families.plugin("zephyrtastic").health_counters
    assert spec["cmd"] == "meshtastic state"
    line = ('uart:~$ meshtastic state\n~S{"v":1,"build":"0123abcd","health":{"up":%d,'
            '"ops":{"tx":%d,"rx_not_for_us":%d},"retry":{"cad_busy":%d},'
            '"warn":{"phone_drop":%d},"err":{"tx_fail":%d,"decode_bad":0}}}*4f2a\n')
    before = line % (100, 4, 0, 0, 0, 0)
    r = counter_delta(line % (160, 9, 7, 3, 300, 0), before, spec)
    assert r["why"] == [], "work, bystander DMs, retries and warnings are not errors"
    assert r["recorded"] == {"ops": {"tx": 5, "rx_not_for_us": 7}, "retry": {"cad_busy": 3},
                             "warn": {"phone_drop": 300}}
    assert counter_delta(line % (160, 9, 0, 0, 0, 1), before, spec)["why"] == ["tx_fail grew by 1 (0 -> 1)"]
    assert "restarted" in counter_delta(line % (5, 0, 0, 0, 0, 0), before, spec)["why"][0]
    old = '~S{"v":1,"build":"0123abcd","lora":{"chip":1}}*4f2a\n'
    assert counter_delta(line % (160, 9, 0, 0, 0, 1), old, spec)["why"] == [], \
        "firmware from before the counters is not judged"
