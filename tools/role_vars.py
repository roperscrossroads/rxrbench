#!/usr/bin/env python3
"""The variables a role reads, by entry point: what its meta/argument_specs.yml must declare.

An entry point is a task file a play enters the role by (`main`, or `tasks_from`). What it
reads is every variable its tasks template (in `{{ }}`, `{% %}` and the bare expressions of
`when`, `that`, `loop` ...), through the files it includes, less what the role defines
itself (`register`, `set_fact`, task and block `vars`, loop variables, `vars/main.yml`).

Every variable an expression reads counts, whatever its name, found by Jinja's own parser
(so a filter, a test, a Jinja local or a loop variable is never one); what Ansible itself
provides (`hostvars`, `inventory_hostname`, `ansible_*`, `omit`, `lookup` ...) is not an input.
A variable a caller passes (`include_role` with `vars:`, a play's own variable) is an input of
the entry point that reads it and is declared there. `tests/unit/test_role_specs.py` holds
every role to it:

    ./venv/bin/python tools/role_vars.py [role-dir ...]     # what each entry point reads
"""

import glob
import os
import re
import sys

import jinja2
import jinja2.meta
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.plugin_utils import families  # noqa: E402
ROLES = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "*", "roles", "*")

# A layout's own blocks: `firmware`, and those its families declare (docs/LAYOUT.md: a
# family's block, a vocabulary's block), named after the loaded families.
GENERIC = {"firmware", "storage", "routes", "builds"}
#: What Ansible and Jinja provide to every task: never a role's input.
MAGIC = {"hostvars", "groups", "group_names", "inventory_hostname", "inventory_hostname_short",
         "inventory_dir", "inventory_file", "play_hosts", "playbook_dir", "role_path", "role_name",
         "omit", "item", "lookup", "query", "q", "range", "dict", "lipsum", "cycler", "joiner",
         "namespace", "now", "undef", "true", "false", "none", "True", "False", "None"}
# Keys whose value is a bare Jinja expression, not a template.
BARE = {"when", "until", "changed_when", "failed_when", "that", "loop", "with_items",
        "with_dict", "loop_control"}
JINJA = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.S)


class _Any(dict):
    """Every filter and test name is known: the parser is asked what a template reads, not
    whether Ansible would run it."""
    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        return lambda *a, **k: None

    def get(self, key, default=None):
        return self[key]


_ENV = jinja2.Environment(extensions=["jinja2.ext.do", "jinja2.ext.loopcontrols"])
_ENV.filters, _ENV.tests = _Any(), _Any()


def names_read(text, bare=False):
    """The variables a template (or, `bare`, a bare expression such as a `when`) reads."""
    src = "{{ (" + text + ") }}" if bare and not JINJA.search(text) else text
    try:
        return jinja2.meta.find_undeclared_variables(_ENV.parse(src))
    except jinja2.TemplateSyntaxError as e:
        raise ValueError(f"cannot parse {text!r}: {e}") from None
INCLUDE = ("ansible.builtin.include_tasks", "include_tasks", "ansible.builtin.import_tasks",
           "import_tasks")
TEMPLATE = ("ansible.builtin.template", "template")


_blocks = None


def blocks():
    """The names a layout may declare a block under: each loaded family's name and its first
    word (family `<f>_settings` declares the `<f>` block). A vocabulary's block is named after a
    family of it; a vocabulary's own name can be a plain word (`files`), so it is not one."""
    global _blocks
    if _blocks is None:
        _blocks = GENERIC | {w for f in families.FAMILIES for w in (f, f.split("_")[0])}
    return _blocks


def ours(name):
    """A variable some caller provides: anything Ansible itself does not."""
    return name not in MAGIC and not name.startswith("ansible_")


def _expressions(node, key=None):
    """(text, bare) for every template and bare expression in a task file's data."""
    if isinstance(node, str):
        if key in BARE:
            yield node, True
        elif JINJA.search(node):
            yield node, False
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _expressions(v, k)
    elif isinstance(node, list):
        # a list under `when` or `that` is expressions; under `loop` it is data (its items
        # are plain values, templated or not)
        for v in node:
            yield from _expressions(v, key if key in ("when", "that", "changed_when",
                                                      "failed_when", "until") else None)


def _defined(node, out):
    if isinstance(node, dict):
        if isinstance(node.get("register"), str):
            out.add(node["register"])
        for k in ("ansible.builtin.set_fact", "set_fact"):
            if isinstance(node.get(k), dict):
                out.update(node[k])
        if isinstance(node.get("vars"), dict) and not any(k in node for k in
                                                          ("ansible.builtin.include_role",
                                                           "include_role")):
            out.update(node["vars"])
        lc = node.get("loop_control")
        out.add(lc.get("loop_var", "item") if isinstance(lc, dict) else "item")
        for v in node.values():
            _defined(v, out)
    elif isinstance(node, list):
        for v in node:
            _defined(v, out)


def _includes(node, role):
    """The role's own task files this one includes and the templates it renders (a templated
    name: every file it can be)."""
    if isinstance(node, dict):
        for keys, sub, field in ((INCLUDE, "tasks", "file"), (TEMPLATE, "templates", "src")):
            for k in keys:
                target = node.get(k)
                if isinstance(target, dict):
                    target = target.get(field)
                if isinstance(target, str):
                    pattern = JINJA.sub("*", target)
                    yield from sorted(glob.glob(os.path.join(role, sub, pattern)))
        for v in node.values():
            yield from _includes(v, role)
    elif isinstance(node, list):
        for v in node:
            yield from _includes(v, role)


def _load(path):
    with open(path) as f:
        return yaml.safe_load(f) or []


def role_locals(role):
    """What the role defines itself, in any of its files: never an argument."""
    out = set()
    for f in glob.glob(os.path.join(role, "tasks", "**", "*.yml"), recursive=True) + \
            glob.glob(os.path.join(role, "handlers", "*.yml")):
        _defined(_load(f), out)
    v = os.path.join(role, "vars", "main.yml")
    if os.path.exists(v):
        out.update(_load(v) or {})
    return out


def file_reads(path, role, local):
    out = set()
    if not path.endswith(".yml"):           # a template: its text is the template
        with open(path) as f:
            node = f.read()
    else:
        node = _load(path)
    exprs = _expressions(node) if path.endswith(".yml") else [(node, False)]
    for text, bare in exprs:
        out.update(n for n in names_read(text, bare) if ours(n) and n not in local)
    return out


def entry_points(role):
    """The task files a play can enter the role by: every top-level task file."""
    return sorted(os.path.splitext(os.path.basename(f))[0]
                  for f in glob.glob(os.path.join(role, "tasks", "*.yml")))


def defaults(role):
    """The role's defaults: its arguments, documented under `main`."""
    path = os.path.join(role, "defaults", "main.yml")
    return set(_load(path) or {}) if os.path.exists(path) else set()


def reads(role, entry, local=None):
    """The variables an entry point reads, through every file it includes or renders."""
    local = role_locals(role) if local is None else local
    seen, todo, out = set(), [os.path.join(role, "tasks", entry + ".yml")], set()
    while todo:
        f = todo.pop()
        if f in seen or not os.path.exists(f):
            continue
        seen.add(f)
        out |= file_reads(f, role, local)
        if f.endswith(".yml"):
            todo.extend(_includes(_load(f), role))
    return out


def roles():
    return sorted(glob.glob(ROLES))


def main(argv):
    for role in argv or roles():
        local = role_locals(role)
        print(os.path.relpath(role, REPO))
        for e in entry_points(role):
            print(f"  {e}: {' '.join(sorted(reads(role, e, local))) or '-'}")


if __name__ == "__main__":
    main(sys.argv[1:])
