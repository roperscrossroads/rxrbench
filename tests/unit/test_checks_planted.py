"""Every tree check, run against a planted breach in a temporary tree, fails with the requirement
it protects and the ways out (docs/ARCHITECTURE.md#the-tools-own-tests; the names check has its own,
test_names.py). Each check is the real one, pointed at the temporary tree through the paths it
reads; nothing here copies a check's logic."""
import os

import pytest

from tests.support.requirement import DO_NOT
from tests.unit import test_chip_flow as tcf
from tests.unit import test_families as tf
from tests.unit import test_gate as tg
from tests.unit import test_hygiene as th
from tests.unit import test_portability as tp
from tests.unit import test_registry_contract as trc
from tests.unit import test_role_specs as trs
from tests.unit import test_region_flow as tsf
from tests.unit import test_terms as tt


def fails_with_its_requirement(check, *args):
    """The check's failure text: what matched, the requirement, and every way out."""
    with pytest.raises(AssertionError) as e:
        check(*args)
    text = str(e.value)
    for part in ("Protects:", "If the requirement is broken:",
                 "If it holds and this is a false alarm:", DO_NOT):
        assert part in text, f"{check.__name__}: no {part!r} in\n{text}"
    return text


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---- core-portability P3, P4 (test_portability.py) ------------------------------------------

@pytest.fixture
def core_tree(tmp_path, monkeypatch):
    core = tmp_path / "core"
    monkeypatch.setattr(tp, "REPO", str(tmp_path))
    monkeypatch.setattr(tp, "CORE", str(core))
    monkeypatch.setattr(tp, "OWN", str(core / "plugins" / "plugin_utils" / "families"))
    monkeypatch.setattr(tp, "KNOWN", {"somefamily"})
    monkeypatch.setattr(tp, "PLUGINS", ["someplugin"])
    return core


def test_a_name_compared_in_core_fails_with_p3(core_tree):
    write(core_tree, "plugins/module_utils/x.py", "def f(b):\n    return b.kind == 'somefamily'\n")
    assert "P3" in fails_with_its_requirement(tp.test_core_python_never_compares_a_plugins_name)


def test_a_name_in_a_core_condition_fails_with_p3(core_tree):
    write(core_tree, "playbooks/x.yml", "- hosts: all\n  tasks:\n    - debug: {}\n"
                                        "      when: bench_family == 'somefamily'\n")
    assert "P3" in fails_with_its_requirement(tp.test_core_plays_never_branch_on_a_plugins_name)


def test_a_plugin_imported_by_core_fails_with_p4(core_tree):
    write(core_tree, "plugins/module_utils/x.py",
          "from ansible_collections.rxrbench.someplugin.plugins.module_utils import y\n")
    assert "P4" in fails_with_its_requirement(tp.test_core_imports_no_plugin)


def test_a_plugin_role_named_by_core_fails_with_p4(core_tree):
    write(core_tree, "playbooks/x.yml", "- hosts: all\n  roles: [rxrbench.someplugin.board]\n")
    assert "P4" in fails_with_its_requirement(tp.test_core_plays_name_no_plugin_role)


# ---- core-portability P2 and the families as data (test_families.py) ------------------------

def test_one_familys_shape_in_generic_code_fails_with_p2(tmp_path, monkeypatch):
    monkeypatch.setattr(tf, "REPO", str(tmp_path))
    write(tmp_path, "tools/x.py", "intents = hostvars.get(\"zephyrtastic\")\n")
    assert "P2" in fails_with_its_requirement(tf.test_generic_code_does_not_know_one_familys_shape)


def test_a_bench_host_module_importing_the_families_fails_with_its_requirement(tmp_path, monkeypatch):
    mu = tmp_path / "core" / "module_utils"
    monkeypatch.setattr(tf, "MU", str(mu))
    for name in tf.CONTROLLER_ONLY:
        write(mu, name, "from ..plugin_utils import families\n")
    write(tmp_path / "core", "modules/x.py", "from ..plugin_utils import families\n")
    text = fails_with_its_requirement(tf.test_code_that_runs_on_a_bench_host_does_not_import_the_families)
    assert "modules/x.py" in text


# ---- the registry contract (test_registry_contract.py) --------------------------------------

def test_a_registry_key_read_and_not_recorded_fails_with_the_contract(tmp_path, monkeypatch):
    monkeypatch.delenv("RATCHET_RECORD", raising=False)
    monkeypatch.setattr(trc, "REPO", str(tmp_path))
    monkeypatch.setattr(trc, "READERS", ("reader.py",))
    write(tmp_path, "reader.py", "def f(e):\n    return e['match'], e.get('newkey')\n")
    monkeypatch.setattr(trc, "RECORD", str(write(tmp_path, "keys.txt", "reader.py: match\n")))
    text = fails_with_its_requirement(trc.test_every_registry_key_a_reader_uses_is_recorded_and_documented)
    assert "newkey" in text and "REGISTRY.md" in text


# ---- role argument specs (test_role_specs.py) -----------------------------------------------

def test_an_input_a_role_reads_and_does_not_declare_fails_with_its_requirement(tmp_path):
    role = tmp_path / "roles" / "r"
    write(role, "tasks/main.yml", "- ansible.builtin.debug:\n    msg: \"{{ bench_thing }}\"\n")
    write(role, "meta/argument_specs.yml",
          "argument_specs:\n  main:\n    short_description: r\n    options: {}\n")
    text = fails_with_its_requirement(trs.test_a_role_declares_what_each_entry_point_reads, str(role))
    assert "bench_thing" in text


# ---- the write gate (test_gate.py) ------------------------------------------------------------

def test_a_writing_module_without_the_gate_fails_with_its_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "COLLECTIONS", str(tmp_path))
    write(tmp_path, "c/plugins/modules/writer.py", "def main():\n    p = module.params\n")
    fails_with_its_requirement(tg.test_every_writing_module_takes_the_go_ahead_and_checks_it_first,
                               "writer")


def test_a_writing_play_without_the_gate_fails_with_its_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "REPO", str(tmp_path))
    write(tmp_path, "collections/ansible_collections/rxrbench/core/playbooks/w.yml",
          "- hosts: all\n  tasks:\n    - rxrbench.core.board_flash: {}\n")
    fails_with_its_requirement(tg.test_every_writing_play_passes_the_gate_before_its_first_write, "w")


def test_a_writing_task_without_the_go_ahead_fails_with_its_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "COLLECTIONS", str(tmp_path))
    monkeypatch.setattr(tg, "REPO", str(tmp_path))
    write(tmp_path, "p/roles/board/tasks/x.yml",
          f"- name: write\n  rxrbench.p.{tg.WRITERS_AT_WRITE[0]}:\n    board: b\n")
    fails_with_its_requirement(tg.test_every_task_that_can_write_through_them_passes_the_go_ahead)


# ---- the chip and region flows (test_region_flow.py, test_chip_flow.py) -------------------

def test_a_route_command_in_a_flash_module_fails_with_its_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(tsf, "MODULES", str(tmp_path))
    for name in tsf.FLASH_MODULES:
        write(tmp_path, name, "DOCUMENTATION = 'erase_region'\n"
                              "from ansible.module_utils.basic import AnsibleModule\n")
    write(tmp_path, "region_erase.py", "from ansible.module_utils.basic import AnsibleModule\n"
                                      "argv = ['esptool', 'erase_region']\n")
    write(tmp_path, "chip_backup.py", "from ansible.module_utils.basic import AnsibleModule\n"
                                      "argv = ['python', 'chip_blocks.py', 'read']\n")
    text = fails_with_its_requirement(tsf.test_the_flash_modules_hold_no_routes_code)
    assert "#chip-and-region-flows: each route's chip access lives in one place" in text
    assert "region_erase.py: names erase_region" in text
    assert "chip_backup.py: names chip_blocks" in text
    assert "region_backup.py" not in text.split("Protects:")[0], "the documentation is not code"


def test_a_copy_of_the_chip_flow_in_a_route_fails_with_its_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(tcf, "FLASH", str(tmp_path))
    for name in tcf.CHIP_ROUTE_FILES:
        write(tmp_path, name, "import os\n")
    write(tmp_path, "simchip.py", "import gzip\nwith gzip.open('x', 'wb') as g:\n    pass\n")
    text = fails_with_its_requirement(tcf.test_the_chip_routes_hold_no_copy_of_the_flow)
    assert "#chip-and-region-flows at the chip level: one backup and one restore flow" in text
    assert "simchip.py: gzip" in text and "swdchip.py" not in text.split("Protects:")[0]


# ---- the backup terms (test_terms.py) ---------------------------------------------------------

def test_an_old_name_for_the_region_level_fails_with_its_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(tt, "REPO", str(tmp_path))
    write(tmp_path, "docs/x.md", "Run settings_backup first.\nZephyr's settings shell stays.\n")
    write(tmp_path, "CHANGELOG.md", "- settings_backup was renamed\n")
    monkeypatch.setattr(tt, "tracked", lambda: ["docs/x.md", "CHANGELOG.md"])
    text = fails_with_its_requirement(tt.test_the_old_names_for_the_region_level_are_gone)
    assert "docs/x.md:1" in text and "docs/x.md:2" not in text and "CHANGELOG" not in text


def test_an_optional_role_spelled_the_old_way_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(tt, "REPO", str(tmp_path))
    write(tmp_path, "docs/y.md", "`[{label, kind, start, size, role?: settings}]`\n")
    monkeypatch.setattr(tt, "tracked", lambda: ["docs/y.md"])
    assert "docs/y.md:1" in fails_with_its_requirement(
        tt.test_the_old_names_for_the_region_level_are_gone)


# ---- hygiene: dates, voice, settings, site paths (test_hygiene.py) --------------------------

@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A tree of the files written into it, as test_hygiene.tracked() would list them."""
    monkeypatch.setattr(th, "REPO", str(tmp_path))
    monkeypatch.setattr(th, "tracked", lambda: sorted(
        os.path.relpath(os.path.join(d, f), tmp_path) for d, _s, fs in os.walk(tmp_path) for f in fs))
    return tmp_path


def test_a_dated_note_fails_with_its_requirement(tree):
    write(tree, "docs/x.md", "On 11-03 the board stopped answering.\n")
    fails_with_its_requirement(th.test_no_dated_notes_in_the_tree)


def test_a_first_person_comment_fails_with_its_requirement(tree):
    write(tree, "tools/x.py", "# we write the image here\n")
    fails_with_its_requirement(th.test_comments_are_in_the_third_person_and_name_nothing_private)


def test_a_setting_read_and_not_in_env_example_fails_with_its_requirement(tree):
    write(tree, ".env.example", "#RXRBENCH_SITE=site\n")
    write(tree, "tools/x.py", "import os\nos.environ.get(\"RXRBENCH_NEW_THING\")\n")
    assert "RXRBENCH_NEW_THING" in fails_with_its_requirement(th.test_every_setting_is_in_env_example)


def test_a_site_path_outside_site_py_fails_with_its_requirement(tree):
    write(tree, "tools/x.py", "path = os.path.join(root, 'x', '/site/registry.yaml')\n")
    fails_with_its_requirement(th.test_site_paths_come_from_one_place)
