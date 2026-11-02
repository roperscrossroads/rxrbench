"""Every role's meta/argument_specs.yml says what its entry points read, and nothing else.

Ansible checks a role's variables against the spec of the entry point a play enters it by, and
antsibull-docs documents them from it; both are only as good as the spec. What each entry point
reads is tools/role_vars.py's to find.
"""

import collections
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import role_vars  # noqa: E402
from tests.support.requirement import breach  # noqa: E402

ROLES = role_vars.roles()
PROTECTS = ("a role's argument spec documents and validates every input of each entry point, "
            "no more (docs/ARCHITECTURE.md#the-tools-own-tests; tools/role_vars.py finds the reads)")


def _spec(role):
    with open(os.path.join(role, "meta", "argument_specs.yml")) as f:
        return yaml.safe_load(f)["argument_specs"]


def _expected(role):
    """Entry point -> what it must declare. A plugin role's main does nothing (its task files are
    entered by name), so it has no spec unless it reads something."""
    local = role_vars.role_locals(role)
    core = os.sep + os.path.join("core", "roles") + os.sep in role
    out = {}
    for entry in role_vars.entry_points(role):
        reads = role_vars.reads(role, entry, local)
        if entry == "main":
            reads |= role_vars.defaults(role)
            if not reads and not core:
                continue
        out[entry] = reads
    return out


def test_there_are_roles():
    assert len(ROLES) >= 10


@pytest.mark.parametrize("role", ROLES, ids=lambda r: os.path.relpath(r, role_vars.REPO))
def test_a_role_declares_what_each_entry_point_reads(role):
    spec, want = _spec(role), _expected(role)
    assert sorted(spec) == sorted(want), "entry points: a spec for each task file a play enters"
    for entry, reads in want.items():
        declared = set(spec[entry].get("options") or {})
        assert not reads - declared, breach(
            [f"{entry}: read, not declared: {sorted(reads - declared)}"], PROTECTS,
            "declare each input under the entry point's options with its type and what it is, "
            "whatever its name and whoever sets it (a play, an include's vars, the layout)")
        assert not declared - reads, breach(
            [f"{entry}: declared, never read: {sorted(declared - reads)}"], PROTECTS,
            "remove the option the entry point does not read")


@pytest.mark.parametrize("role", ROLES, ids=lambda r: os.path.relpath(r, role_vars.REPO))
def test_every_option_has_a_type_and_says_what_it_is(role):
    for entry, s in _spec(role).items():
        assert s.get("short_description"), entry
        assert s.get("description"), f"{entry}: antsibull-docs refuses an entry point without one"
        for name, o in (s.get("options") or {}).items():
            assert o.get("type"), f"{entry}.{name}: no type"
            text = o.get("description")
            text = " ".join(text) if isinstance(text, list) else text or ""
            assert len(text) > 15 and "TODO" not in text, f"{entry}.{name}: {text!r}"
            assert "default" not in o, (f"{entry}.{name}: a spec's default is documentation only "
                                        "and drifts; the role's defaults/main.yml holds it")


def test_a_variable_has_one_type_in_every_role():
    types = collections.defaultdict(set)
    for role in ROLES:
        for entry, s in _spec(role).items():
            for name, o in (s.get("options") or {}).items():
                types[name].add(o["type"])
    assert not {n: t for n, t in types.items() if len(t) > 1}


def test_the_reader_sees_includes_and_templates():
    """The parts of role_vars the specs lean on: an include brings its file's reads, a template
    its text's, and what a role registers is not an argument."""
    board = next(r for r in ROLES if r.endswith(os.path.join("core", "roles", "board")))
    main = role_vars.reads(board, "main")
    assert "bench_repo" in main                        # read in forget_bootloader.yml, included
    assert "board_flashed" not in main                 # registered by the role itself
    broker = next(r for r in ROLES if r.endswith("serial_broker"))
    assert "serial_broker_devreg" in role_vars.reads(broker, "main")   # read in a template only


def test_every_name_a_template_reads_counts_and_nothing_else_does():
    """Jinja's parser finds the reads: any name (not only bench_/board_), never a filter, a
    test, a Jinja local, a list under `loop`, or what Ansible provides."""
    read = role_vars.names_read("{{ step.cmd | default('') }}{% for x in hosts %}{{ x }}{% endfor %}")
    assert read == {"step", "hosts"}
    assert role_vars.names_read("board_x is changed and locate_seconds | int > 0", bare=True) \
        == {"board_x", "locate_seconds"}
    node = {"loop": ["simboard.py", "{{ helper }}"], "when": ["ready"]}
    found = set().union(*(role_vars.names_read(t, b) for t, b in role_vars._expressions(node)))
    assert found == {"helper", "ready"}
    assert not {n for n in ("hostvars", "inventory_hostname", "ansible_check_mode", "omit")
                if role_vars.ours(n)}
