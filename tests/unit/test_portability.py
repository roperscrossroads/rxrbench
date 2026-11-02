"""Core never chooses by a family's or plugin's name and imports no plugin.

Protects core-portability P3 and P4 (docs/ARCHITECTURE.md#core-portability). These read the code's
structure, not its words: a comparison is found whatever its variables are called, and a
name in a comment or a message is not a comparison.
"""
import ast
import glob
import os
import re

import pytest

import yaml

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common as flash_common
from ansible_collections.rxrbench.core.plugins.plugin_utils import families
from tests.support.requirement import breach

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NS = os.path.join(REPO, "collections", "ansible_collections", "rxrbench")
CORE = os.path.join(NS, "core")
PLUGINS = sorted(d for d in os.listdir(NS) if d != "core" and os.path.isdir(os.path.join(NS, d)))
# core's own families are core's to name, in their own package
OWN = os.path.join(CORE, "plugins", "plugin_utils", "families")


def _board_types():
    out = set()
    for plugin in PLUGINS:
        path = os.path.join(NS, plugin, "catalog", "boards.yml")
        if os.path.exists(path):
            with open(path) as fh:
                data = yaml.safe_load(fh) or {}
            out |= set(data.get("boards") or {})
    return out


# What a flasher can do for a board (bench_can, flash/common.py CAPABILITIES): core's own
# words, never a plugin's name.
CORE_WORDS = {w for ws in flash_common.CAPABILITIES.values() for w in ws}
# What a plugin brings: its collection, its families, their vocabularies, its board types.
KNOWN = ({f for f, row in families.FAMILIES.items() if row.get("plugin") not in (None, "rxrbench.core")}
         | {r.get("vocabulary") for r in families.FAMILIES.values()
            if r.get("plugin") not in (None, "rxrbench.core")}
         | set(PLUGINS) | {f"rxrbench.{p}" for p in PLUGINS} | _board_types()) - {None} - CORE_WORDS
P3 = ("core-portability P3: core never chooses by a family's, plugin's or board type's name "
      "(docs/ARCHITECTURE.md#core-portability)")
P3_FIX = ("make the difference a Family hook, a field of the family's spec (bench_families) or "
          "catalog data, and ask for it (docs/PLUGINS.md)")
P4 = "core-portability P4: core imports no plugin (docs/ARCHITECTURE.md#core-portability)"
P4_FIX = ("what core needs of a plugin reaches it through the plugin loader and a Family hook, "
          "or as data published to the host (familyspec.py)")


def core_python():
    for path in glob.glob(os.path.join(CORE, "**", "*.py"), recursive=True):
        if not path.startswith(OWN + os.sep):
            yield path
    for top in ("tools", "devreg"):
        yield from glob.glob(os.path.join(REPO, top, "**", "*.py"), recursive=True)


def _literals(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for e in node.elts:
            yield from _literals(e)


def comparisons(source, known=None):
    """(line, name) for every comparison of a known name: ==, !=, in, not in, match cases.
    `known`: the names (default KNOWN, read when called, not when this was defined)."""
    known = KNOWN if known is None else known
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            for side in [node.left] + node.comparators:
                out += [(node.lineno, s) for s in _literals(side) if s in known]
        elif isinstance(node, ast.match_case) and isinstance(node.pattern, ast.MatchValue):
            out += [(node.pattern.lineno, s) for s in _literals(node.pattern.value) if s in known]
    return out


def imports(source):
    """(line, module) for every import from another collection of the namespace."""
    out = []
    for node in ast.walk(ast.parse(source)):
        mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for m in mods:
            parts = m.split(".")
            if parts[:2] == ["ansible_collections", "rxrbench"] and len(parts) > 2 \
                    and parts[2] != "core":
                out.append((node.lineno, m))
    return out


def _rel(path):
    return os.path.relpath(path, REPO)


def test_core_python_never_compares_a_plugins_name():
    hits = [f"{_rel(p)}:{n}: {name!r}" for p in core_python()
            for n, name in comparisons(open(p, encoding="utf-8").read())]
    assert not hits, breach(["a plugin's name compared in core:"] + hits, P3, P3_FIX)


QUOTED = re.compile(r"""['"]([A-Za-z0-9_.-]+)['"]""")
EXPR_KEYS = ("when", "that", "failed_when", "changed_when", "until")


def _yaml_exprs(node, key=None):
    if isinstance(node, str) and key in EXPR_KEYS:
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _yaml_exprs(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _yaml_exprs(v, key)


def test_core_plays_never_branch_on_a_plugins_name():
    hits = []
    for path in glob.glob(os.path.join(CORE, "**", "*.yml"), recursive=True):
        if os.sep + "catalog" + os.sep in path or os.sep + "meta" + os.sep in path:
            continue
        with open(path) as fh:
            data = yaml.safe_load(fh)
        for e in _yaml_exprs(data):
            hits += [f"{_rel(path)}: {e.strip()[:90]}" for m in QUOTED.findall(e) if m in KNOWN]
    assert not hits, breach(["a plugin's name in a core condition:"] + hits, P3, P3_FIX)


def test_core_imports_no_plugin():
    hits = [f"{_rel(p)}:{n}: {m}" for p in core_python()
            for n, m in imports(open(p, encoding="utf-8").read())]
    assert not hits, breach(["core imports a plugin:"] + hits, P4, P4_FIX)


def test_core_plays_name_no_plugin_role():
    pat = re.compile(r"\brxrbench\.(%s)\." % "|".join(map(re.escape, PLUGINS)))
    hits = []
    for path in glob.glob(os.path.join(CORE, "**", "*.yml"), recursive=True):
        for n, line in enumerate(open(path, encoding="utf-8"), 1):
            if pat.search(line.split("#", 1)[0]):
                hits.append(f"{_rel(path)}:{n}: {line.strip()[:90]}")
    assert not hits, breach(["core names a plugin's role, module or play:"] + hits, P4, P4_FIX)


def test_a_comparison_is_found_whatever_the_variable_is_called():
    src = "def f(x):\n    if x.kind == 'meshtastic':\n        return 1\n    return x in ('linux_rauc', 'y')\n"
    assert {n for _, n in comparisons(src, {"meshtastic", "linux_rauc"})} == {"meshtastic", "linux_rauc"}
    assert comparisons("log('meshtastic node')\n# == 'meshtastic'\n", {"meshtastic"}) == []


@pytest.mark.plugins
def test_the_known_names_are_a_plugins_and_not_cores_words():
    assert {"meshtastic", "linux_rauc", "rxrbench.mesh", "heltec-v4"} <= KNOWN
    assert "regions" in CORE_WORDS and not KNOWN & ({"boards"} | CORE_WORDS)


def test_an_import_from_a_plugin_is_found():
    src = "from ansible_collections.rxrbench.mesh.plugins.module_utils import x\nimport os\n"
    assert imports(src) == [(1, "ansible_collections.rxrbench.mesh.plugins.module_utils")]
    assert imports("from ansible_collections.rxrbench.core.plugins import y\n") == []
