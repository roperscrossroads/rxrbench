"""Every module's and plugin's DOCUMENTATION, EXAMPLES and RETURN are YAML that parses.
ansible-lint does not fail on a doc it cannot read (it logs an ERROR and passes), and
ansible-doc then shows nothing for the module; a callback, lookup or inventory plugin whose doc
does not parse is not loaded, and every play that enables it stops. An unquoted `: ` inside a
description is the usual cause."""
import ast
import glob
import os

import pytest
import yaml

PLUGINS = os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections",
                       "rxrbench", "*", "plugins")
MODULES = sorted(glob.glob(os.path.join(PLUGINS, "modules", "*.py")))
#: the other plugin types that carry their documentation in the .py (a filter's is a sidecar)
OTHERS = sorted(p for kind in ("callback", "lookup", "inventory")
                for p in glob.glob(os.path.join(PLUGINS, kind, "*.py"))
                if not os.path.basename(p).startswith("__"))


@pytest.mark.parametrize("path", MODULES + OTHERS,
                         ids=lambda p: os.path.join(os.path.basename(os.path.dirname(p)), os.path.basename(p)))
def test_the_module_docs_parse(path):
    with open(path) as f:
        tree = ast.parse(f.read())
    docs = {n.targets[0].id: n.value.value for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id in ("DOCUMENTATION", "EXAMPLES", "RETURN")}
    assert "DOCUMENTATION" in docs
    for name, text in docs.items():
        yaml.safe_load(text)
