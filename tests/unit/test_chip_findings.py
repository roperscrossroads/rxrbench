"""What the plan reports from the survey's record of a chip (docs/ARCHITECTURE.md#partitions-and-the-write-rule):
other firmware in an app slot, data every app of a managed chip shares, with the survey's date."""
import importlib.util
import os

from ansible_collections.rxrbench.core.plugins.filter.chip import chip_findings
from tests.unit import esptables

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    "usb_survey", os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core",
                               "plugins", "modules", "usb_survey.py"))
survey = importlib.util.module_from_spec(spec)
spec.loader.exec_module(survey)

MANAGED = list(esptables.MANAGED_4MB) + [esptables.row(0, 0x10, 0x160000, 0x100000, "camstr"),
                                          esptables.row(0, 0x11, 0x260000, 0xE0000, "wifite")]


def facts_of(rows, images, boot_seq):
    from ansible_collections.rxrbench.core.plugins.module_utils import esppart
    blob = esptables.chip(rows, images=images, boot_seq=boot_seq)
    rows = esppart.parse(blob[0x8000:0x8C00])
    rec = {"manager": esppart.manager(rows), **survey.survey_inventory(rows, lambda o, n: blob[o:o + n])}
    return {"probes": {"esptool": {"gathered": "2026-11-07T18:00:00Z", "partitions": rec}}}, rec


def test_the_survey_records_the_boot_app_and_each_regions_owner():
    _facts, rec = facts_of(MANAGED, [0x10000, 0x160000, 0x260000], boot_seq=1)
    by = {r["label"]: r["owner"] for r in rec["inventory"]}
    assert rec["boot"] == "camstr"
    assert by == {"nvs": "shared", "otadata": "shared", "test": "foreign", "camstr": "declared",
                  "wifite": "foreign"}


def test_the_plan_lists_what_the_layout_does_not_own_with_the_surveys_date():
    facts, _rec = facts_of(MANAGED, [0x10000, 0x160000, 0x260000], boot_seq=1)
    found = chip_findings(facts)
    assert any(f.startswith("test at 0x10000 holds other firmware (esp-app-image)") for f in found)
    assert any(f.startswith("nvs at 0x9000 holds data every app on this chip shares") for f in found)
    assert all(f.endswith("as surveyed 2026-11-07T18:00:00Z") for f in found)
    assert not any(f.startswith("camstr") for f in found), "the firmware it runs is the layout's"
    named = chip_findings(facts, ["test", "nvs", "otadata"])
    assert [f.split()[0] for f in named] == ["wifite"]
    assert chip_findings(facts, ["all"]) == []


def test_a_plain_chip_or_no_record_has_nothing_to_report():
    plain = [esptables.row(1, 0x02, 0x9000, 0x5000, "nvs"), esptables.row(1, 0x00, 0xE000, 0x2000, "otadata"),
             esptables.row(0, 0x10, 0x10000, 0x140000, "app0"), esptables.row(0, 0x11, 0x150000, 0x140000, "app1")]
    facts, _rec = facts_of(plain, [0x10000, 0x150000], boot_seq=2)
    assert chip_findings(facts) == []
    assert chip_findings({}) == [] and chip_findings(None) == []


def test_a_family_that_installs_the_manager_owns_what_it_declares_and_says_what_is_there(monkeypatch):
    """Plugins describe, core decides (docs/ARCHITECTURE.md#partitions-and-the-write-rule): a family's chip_regions leaves out what its
    declaration owns and names the chip; a board of a family with no such answer is unchanged."""
    import types
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families, registry
    from ansible_collections.rxrbench.core.plugins.plugin_utils.families import base

    class Keeper(base.Family):
        def chip_regions(self, hostvars, partitions):
            mgr = partitions["manager"]
            return {"owned": [mgr["label"]] + list(hostvars.get("keeps") or []),
                    "describe": f"a keeper in {mgr['label']}"}

    row = dict(families.CORE_FAMILIES["bare"], plugin="keeper")
    mod = types.SimpleNamespace(FAMILIES={"keeper": row}, PLUGINS={"keeper": Keeper()})
    monkeypatch.setattr(registry, "load", lambda names, roots=(): {"fixture.keeper": mod})
    facts, _rec = facts_of(MANAGED, [0x10000, 0x160000, 0x260000], boot_seq=1)
    try:
        families.load(["fixture.keeper"])
        board = {"firmware": {"family": "keeper"}, "keeps": ["nvs", "otadata"]}
        assert chip_findings(facts, [], board)[0] == "the chip carries a keeper in test"
        assert [f.split()[0] for f in chip_findings(facts, [], board)[1:]] == ["wifite"]
        assert chip_findings(facts, ["wifite"], board) == [], "nothing found: no description either"
        unchanged = chip_findings(facts, [], {"firmware": {"family": "bare"}})
        assert unchanged == chip_findings(facts)
    finally:
        monkeypatch.undo()
        families.load()
