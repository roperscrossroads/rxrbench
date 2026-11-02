"""The generated docs' sources: docs/COMMANDS.md's commands table is what the recipes say, and the doc
tools find what they document (tools/docs.py)."""

import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import docs  # noqa: E402

# CI installs the pinned just into the venv; on a machine without one the check cannot run.
JUST = shutil.which("just") or shutil.which(os.path.join(docs.REPO, "venv", "bin", "just"))


@pytest.mark.skipif(not JUST and not os.environ.get("CI"), reason="no just on PATH")
def test_the_readme_commands_table_is_what_the_recipes_say(monkeypatch):
    monkeypatch.setenv("PATH", os.path.dirname(JUST) + os.pathsep + os.environ["PATH"])
    assert docs.commands(check=True), "docs/COMMANDS.md or a plugin README is stale: just docs-readme"


@pytest.mark.skipif(not JUST and not os.environ.get("CI"), reason="no just on PATH")
def test_every_public_recipe_has_a_group_and_a_doc(monkeypatch):
    """A recipe with no [group] would be missing from the README's table."""
    import json
    import subprocess
    dump = json.loads(subprocess.run([JUST, "--dump", "--dump-format", "json"], cwd=docs.REPO,
                                     check=True, capture_output=True, text=True).stdout)
    bare = []
    for name, r in dump["recipes"].items():
        attrs = {k for a in r["attributes"] if isinstance(a, dict) for k in a}
        if name != "default" and not r["private"] and not {"group", "doc"} <= attrs:
            bare.append(name)
    assert not bare, f"recipes without [group] and [doc]: {bare}"


def test_the_synopsis_says_what_a_recipe_takes():
    params = [{"name": "layout", "kind": "singular", "default": None},
              {"name": "region", "kind": "singular", "default": ""},
              {"name": "seconds", "kind": "singular", "default": "60"},
              {"name": "args", "kind": "star", "default": None}]
    assert docs._synopsis("erase", params) == "erase <layout> [region] [seconds=60] [args ...]"


def test_the_doc_tools_see_every_collection_and_its_python():
    assert docs.collections() == ["core", "launcher", "linux", "mesh", "nuttx", "zephyr"]
    pkgs = docs.python_packages()
    assert "ansible_collections.rxrbench.core.plugins.module_utils" in pkgs
    assert not [p for p in pkgs if ".modules" in p], "modules are antsibull-docs' to document"
    assert len(pkgs) == len(set(pkgs))


def test_layout_md_and_the_role_specs_are_what_layout_vars_says():
    assert docs.layout(check=True), "LAYOUT.md or a role spec is stale: just docs-layout"


def test_a_layout_variable_has_one_entry_and_a_type():
    names = [r["name"] for rows in docs.layout_vars().values() for r in rows if "name" in r]
    assert len(names) == len(set(names)), "one home per variable"
    for rows in docs.layout_vars().values():
        for r in rows:
            assert r.get("text") and ("label" in r or r.get("type")), r


def test_the_readme_names_every_plugin_recipe_beside_its_plugin():
    """docs/COMMANDS.md's plugin table names each recipe a plugin's .just file defines, on that
    plugin's row: a newcomer told to run `just sim-up` finds where it is documented."""
    import glob
    import re
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    readme = open(os.path.join(repo, "docs", "COMMANDS.md")).read()
    missing = []
    for path in glob.glob(os.path.join(repo, "collections", "ansible_collections", "rxrbench", "*", "*.just")):
        plugin = os.path.basename(os.path.dirname(path))
        row = next((ln for ln in readme.splitlines() if ln.startswith(f"| [`{plugin}`]")), "")
        for recipe in sorted(set(re.findall(r"^([a-z][a-z0-9-]*)(?:\s[^\n]*)?(?<!:):(?!=)[ \t]*(?:[a-z][a-z0-9 -]*)?$",
                                               open(path).read(), re.M))):
            if f"`{recipe}`" not in row:
                missing.append(f"{plugin}: {recipe}")
    assert missing == [], "docs/COMMANDS.md's plugin table lacks: " + ", ".join(missing)
