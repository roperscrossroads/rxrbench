"""The one gate every write passes: module_utils/gate.py in the modules, tasks/gate.yml in the plays."""
import os
import re

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import gate
from tests.support.requirement import breach

GATE = ("no write without the layout's go-ahead (bench_apply_allowed), checked before the first "
        "write by one gate: module_utils/gate.py in a module, tasks/gate.yml in a play "
        "(CLAUDE.md, docs/ARCHITECTURE.md \"Safety rules, in code\")")
GATE_FIX = ("take **gate.ARGS and call gate.check(module) before anything is written (right after "
            "the parameters are read, or immediately before the write in a module that also reads); "
            "a play includes tasks/gate.yml before its first writing task")



REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COLLECTIONS = os.path.join(REPO, "collections", "ansible_collections", "rxrbench")


def module_path(name):
    """A writing module's source, in core or in a bundled plugin collection."""
    found = [p for p in (os.path.join(COLLECTIONS, c, "plugins", "modules", name + ".py")
                         for c in sorted(os.listdir(COLLECTIONS))) if os.path.exists(p)]
    assert len(found) == 1, (name, found)
    return found[0]

WRITERS = ("board_flash", "smp_flash", "swd_flash", "bootloader_flash", "region_erase", "region_restore",
           "chip_restore", "card_write", "rauc_install", "launcher_apply", "launcher_install",
           "route_enroll", "linux_files")
WRITING_PLAYS = ("apply", "boot", "restore-regions", "restore-chip", "keys-restore", "erase", "switch", "card")


class Module:
    def __init__(self, check_mode, allowed):
        self.check_mode, self.params, self.failed = check_mode, {"apply_allowed": allowed}, None

    def fail_json(self, msg):
        self.failed = msg
        raise SystemExit(msg)


def test_a_plan_passes_and_a_write_needs_the_go_ahead():
    gate.check(Module(check_mode=True, allowed=False))
    gate.check(Module(check_mode=False, allowed=True))
    with pytest.raises(SystemExit, match="bench_apply_allowed"):
        gate.check(Module(check_mode=False, allowed=False))


@pytest.mark.parametrize("name", WRITERS)
def test_every_writing_module_takes_the_go_ahead_and_checks_it_first(name):
    src = open(module_path(name)).read()
    assert "**gate.ARGS" in src, breach([f"{name}: no **gate.ARGS"], GATE, GATE_FIX)
    assert "    p = module.params\n    gate.check(module)" in src, breach(
        [f"{name}: the gate is not the first thing after the parameters are read"], GATE, GATE_FIX,
        "a module that must read before it can tell whether it writes belongs in "
        "WRITERS_AT_WRITE here, with the reason in a comment")


@pytest.mark.parametrize("name", WRITING_PLAYS)
def test_every_writing_play_passes_the_gate_before_its_first_write(name):
    src = open(os.path.join(REPO, "collections/ansible_collections/rxrbench/core/playbooks", name + ".yml")).read()
    assert "include_tasks: tasks/gate.yml" in src, breach([f"{name}: no tasks/gate.yml"], GATE, GATE_FIX)
    first_write = re.search(r"rxrbench\.core\.\w+:|include_role:", src)
    assert first_write and src.index("tasks/gate.yml") < first_write.start(), breach(
        [f"{name}: a module or role runs before the gate"], GATE, GATE_FIX,
        "a read-only task before the gate (a lookup, a facts read) is fine if it cannot write: "
        "then this check is wrong for it; fix the check, with the case as a test")

#: Modules that also read (an identity export, a radio read, a locate blink): the gate stands
#: immediately before the first write, so a read, a plan or a board that already matches passes.
WRITERS_AT_WRITE = ("meshtastic_radio", "meshtastic_config", "meshtastic_identity", "meshtastic_net_config",
                    "meshcore_config", "meshcore_identity", "meshcore_radio", "meshcore_storage",
                    "zephyrtastic_attach", "zephyrtastic_intents", "zephyrtastic_led", "linux_host_key")


@pytest.mark.parametrize("name", WRITERS_AT_WRITE)
def test_a_module_that_also_reads_checks_the_go_ahead_before_it_writes(name):
    src = open(module_path(name)).read()
    assert "**gate.ARGS" in src and "  apply_allowed:\n" in src, breach(
        [f"{name}: no **gate.ARGS or apply_allowed documented"], GATE, GATE_FIX)
    assert src.count("gate.check(module)") == 1, breach(
        [f"{name}: gate.check(module) not exactly once"], GATE, GATE_FIX)


def test_every_task_that_can_write_through_them_passes_the_go_ahead():
    call = re.compile(r"^( *)rxrbench\.\w+\.(" + "|".join(WRITERS_AT_WRITE) + r"):\n((?:\1 +.*\n)*)", re.M)
    missing = []
    for root, _dirs, files in os.walk(COLLECTIONS):
        for f in files:
            if not f.endswith(".yml") or f.endswith("_read.yml") or f == "locate_shell.yml":
                continue
            path = os.path.join(root, f)
            for m in call.finditer(open(path).read()):
                if "apply_allowed:" not in m.group(3):
                    missing.append(f"{os.path.relpath(path, REPO)}: {m.group(2)}")
    assert not missing, breach(["a writing task without apply_allowed:"] + missing, GATE,
                               "pass apply_allowed: \"{{ bench_apply_allowed | default(false) }}\"")
