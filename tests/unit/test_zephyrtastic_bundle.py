"""A layout may name a zephyrtastic class by its number or by its bundle name (the catalog's
`name`); the tasks and the site's templates get the number as bench_firmware_class."""
from ansible_collections.rxrbench.core.plugins.plugin_utils import families
from ansible_collections.rxrbench.mesh.plugins.plugin_utils.families import zephyrtastic

CLASSES = {1: {"name": "xiao-node"}, 15: {"name": "r8-gateway"}}


def test_a_class_is_named_by_number_or_by_bundle():
    assert zephyrtastic.resolve_class(15, CLASSES) == 15
    assert zephyrtastic.resolve_class("15", CLASSES) == 15
    assert zephyrtastic.resolve_class("r8-gateway", CLASSES) == 15
    assert zephyrtastic.resolve_class("xiao-node", CLASSES) == 1


def test_a_name_or_number_the_catalog_lacks_names_nothing():
    for v in (2, "2", "r8-courier", "", None, True):
        assert zephyrtastic.resolve_class(v, CLASSES) is None, v


def _plugin():
    return families.plugin("zephyrtastic")


def test_the_image_check_accepts_a_bundle_name_and_names_the_choices_when_wrong():
    p = _plugin()
    catalog = {"families": {"zephyrtastic": p.catalog([zephyrtastic.CATALOG])}}
    rec, problems = p.image({"family": "zephyrtastic", "class": "r8-gateway"}, catalog)
    assert rec is not None and problems == []
    rec, problems = p.image({"family": "zephyrtastic", "class": "no-such-bundle"}, catalog)
    assert rec is None and "r8-gateway" in problems[0]


def test_the_board_facts_carry_the_number_and_the_bundle():
    p = _plugin()
    for declared in (15, "r8-gateway"):
        facts = p.board_facts({"firmware": {"family": "zephyrtastic", "class": declared}}, [])
        assert facts["firmware_class"] == 15
        assert facts["firmware_bundle"] == "r8-gateway"
    # another family's board gets neither
    facts = p.board_facts({"firmware": {"family": "meshtastic"}}, [])
    assert "firmware_class" not in facts
