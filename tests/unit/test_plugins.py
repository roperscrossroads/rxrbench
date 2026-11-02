"""Plugin collections: found by name, and usable from a play and from a module on a host."""
import os
import subprocess

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import site
from ansible_collections.rxrbench.core.plugins.plugin_utils import families, registry

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(REPO, "tests", "fixtures", "collections")


def test_a_plugin_is_loaded_by_its_collection_name():
    mods = registry.load(["fixture.widgets"], roots=[FIXTURES])
    assert list(mods) == ["fixture.widgets"]
    assert "widget" in mods["fixture.widgets"].FAMILIES


def test_a_plugin_that_is_not_there_is_refused_by_name():
    with pytest.raises(registry.PluginError, match="no.such"):
        registry.load(["no.such"], roots=[FIXTURES])
    with pytest.raises(registry.PluginError, match="namespace.collection"):
        registry.load(["widgets"], roots=[FIXTURES])


def test_a_family_two_plugins_bring_is_refused(tmp_path):
    twin = tmp_path / "ansible_collections" / "fixture" / "twin" / "plugins" / "plugin_utils"
    twin.mkdir(parents=True)
    (twin / "bench.py").write_text('FAMILIES = {"widget": {}}\n')
    with pytest.raises(registry.PluginError, match="both fixture.widgets and fixture.twin"):
        registry.load(["fixture.widgets", "fixture.twin"], roots=[FIXTURES, str(tmp_path)])


def test_the_sites_plugins_come_from_its_file_or_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("RXRBENCH_PLUGINS", raising=False)
    assert site.plugins(str(tmp_path)) == []
    (tmp_path / "plugins.yml").write_text("plugins: [a.b, c.d]\n")
    assert site.plugins(str(tmp_path)) == ["a.b", "c.d"]
    monkeypatch.setenv("RXRBENCH_PLUGINS", "x.y, z.w")
    assert site.plugins(str(tmp_path)) == ["x.y", "z.w"]
    monkeypatch.setenv("RXRBENCH_PLUGINS", "")
    assert site.plugins(str(tmp_path)) == []


def test_a_play_reaches_a_plugins_step_and_its_module_uses_core():
    """The two things the split rests on, on the pinned ansible-core: a role named by a
    templated collection name, and a plugin's module shipping core's module_utils with it."""
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=os.pathsep.join(
        [os.path.join(REPO, "collections"), FIXTURES]), ANSIBLE_NOCOLOR="1")
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-playbook"), "-i", "localhost,",
                        os.path.join(REPO, "tests", "fixtures", "plugin_play", "play.yml")],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WIDGET widget abc123" in r.stdout


def test_the_docs_of_the_plugins_that_are_not_modules_parse():
    """ansible-lint logs a DOCUMENTATION it cannot read and passes; ansible-doc then shows
    nothing for the plugin. A filter file that holds several filters documents each in a
    sidecar (<filter>.yml beside it): ansible-doc reads a sidecar only when the .py has no
    DOCUMENTATION, so it must have none."""
    import ast
    import glob

    import yaml
    paths = [p for kind in ("lookup", "filter", "inventory")
             for p in glob.glob(os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "*",
                                             "plugins", kind, "*.py"))]
    assert paths
    for path in paths:
        tree = ast.parse(open(path).read())
        docs = {n.targets[0].id: n.value.value for n in tree.body
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in ("DOCUMENTATION", "EXAMPLES", "RETURN")}
        for text in docs.values():
            yaml.safe_load(text)
        if "DOCUMENTATION" in docs:
            continue
        names = [k.value for c in tree.body if isinstance(c, ast.ClassDef) and c.name == "FilterModule"
                 for f in c.body if isinstance(f, ast.FunctionDef) and f.name == "filters"
                 for r in ast.walk(f) if isinstance(r, ast.Return) and isinstance(r.value, ast.Dict)
                 for k in r.value.keys]
        assert names, f"{path}: no DOCUMENTATION and no filters with sidecars"
        for name in names:
            side = os.path.join(os.path.dirname(path), name + ".yml")
            assert os.path.exists(side), f"{path}: filter {name} has no sidecar {side}"
            assert yaml.safe_load(open(side))["DOCUMENTATION"]["name"] == name, side


def test_a_plugins_flasher_says_what_its_devices_can_have_done_and_may_not_shadow_cores(tmp_path):
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families
    try:
        families.load(["rxrbench.launcher"])
        # a Launcher board's chip is read and written whole by esptool; it has no region level
        assert families.flasher_can("launcher") == ["chip"]
        assert families.flasher_can("esptool") == ["regions", "chip"]
        assert families.flasher_can("no-such-flasher") == [] and families.flasher_can(None) == []
        rogue = tmp_path / "ansible_collections" / "fixture" / "rogue" / "plugins" / "plugin_utils"
        rogue.mkdir(parents=True)
        (rogue / "bench.py").write_text('FAMILIES = {}\nFLASHERS = {"esptool": ("chip",)}\n')
        with pytest.raises(registry.PluginError, match="a flasher named 'esptool' is already known"):
            families.load(["fixture.rogue"], roots=[str(tmp_path)])
    finally:
        families.load()



def test_a_refusal_says_when_no_loaded_plugin_brings_the_family():
    """Core alone (core-portability P1): a board may declare a family no plugin manages (it is
    left alone); a refusal about it says why, not only what is missing."""
    from ansible_collections.rxrbench.core.plugins.module_utils import familyspec, reboot
    with pytest.raises(reboot.RebootError, match="no loaded plugin brings this family"):
        reboot.commands("no_such_family")
    assert familyspec.unloaded(next(iter(familyspec.families()))) == ""


def test_a_probe_flashed_family_brings_its_flasher_bearers_and_chip_tasks():
    """fixture.widgets' probechip (core-portability P6): a flasher core does not have, that
    writes over a probe, whose chip a backup, restore and erase reach through the family's own
    task files."""
    import os
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families
    roots = [os.path.join(os.path.dirname(__file__), "..", "fixtures", "collections")]
    try:
        families.load(["fixture.widgets"], roots=roots)
        assert families.flasher_can("probeflash") == ["regions", "chip"]
        assert families.flasher_bearers("probeflash") == ("swd",)
        plugin = families.plugin("probechip")
        assert set(plugin.chip_tasks["probeflash"]) == {"backup", "restore", "erase", "region_backup",
                                                        "region_restore", "region_erase"}
        assert plugin.step_tasks == "step_widget"
    finally:
        families.load()
