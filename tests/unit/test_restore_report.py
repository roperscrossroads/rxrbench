"""The restore play's report of what it wrote, rendered as ansible renders it. Found on hardware
a backup records `hash: null` for a firmware that reports
its build instead, and the report sliced that null: the restore had written the regions, and the
play then failed on its own message."""
import os

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAY = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "playbooks",
                    "restore-regions.yml")


def report(restore):
    (play,) = yaml.safe_load(open(PLAY))
    (task,) = [t for t in play["tasks"] if t["name"] == "What was written"]
    msg = task["ansible.builtin.debug"]["msg"]
    return Templar(loader=DataLoader(), variables={"bench_board": "b1", "restore": restore}).template(
        trust_as_template(msg))


REGIONS = [{"name": "storage"}, {"name": "bulk"}]


@pytest.mark.parametrize("taken_by, said", [
    ({"family": "zephyrtastic", "build": "1117570e", "version": "0.4.47+45", "hash": None}, "1117570e"),
    ({"family": "zephyr_settings", "build": None, "version": None, "hash": "6ac19370aa"}, "6ac19370"),
    ({"family": "x", "build": None, "version": "1.1", "hash": None}, "1.1"),
    ({"family": "x", "build": None, "version": None, "hash": None}, "?"),
    (None, "?"),
])
def test_the_report_names_what_the_backup_was_taken_on(taken_by, said):
    out = report({"path": "/b/1", "taken_by": taken_by, "regions": REGIONS, "left_alone": []})
    assert f"(taken on {said};" in out and "storage, bulk" in out, out


def test_a_backup_matched_by_its_table_alone_says_whose_table():
    """Found on hardware: a stock node's region backup (esp-image) knows only the table the
    board ran, not the image, and the report said `taken on ?`."""
    out = report({"path": "/b/1", "taken_by": {"family": "meshtastic"}, "regions": REGIONS,
                  "left_alone": [], "table_of": {"image": "m-2.7.26", "version": "2.7.26.54e0d8d"}})
    assert "(taken on an image with the partition table of 2.7.26.54e0d8d;" in out, out
    out = report({"path": "/b/1", "taken_by": {"family": "meshtastic"}, "regions": REGIONS,
                  "left_alone": [], "table_of": {"image": "m-2.7.26", "version": None}})
    assert "(taken on an image with the partition table of m-2.7.26;" in out, out
