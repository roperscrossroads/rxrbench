"""Every zephyrtastic class is built for a Zephyr board its board type names: the survey and
enroll find a board's type by the board name its firmware reports, so a type that lists
another board's name is never found (enroll then asks for `policy.board_tag` by hand)."""
import os

import yaml

MESH = os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections",
                    "rxrbench", "mesh", "catalog")


def test_each_class_board_is_named_by_its_board_type():
    with open(os.path.join(MESH, "boards.yml")) as fh:
        boards = yaml.safe_load(fh)["boards"]
    with open(os.path.join(MESH, "zephyrtastic.yml")) as fh:
        classes = yaml.safe_load(fh)["classes"]
    wrong = []
    for n, c in classes.items():
        tag, board = c.get("board_tag"), (c.get("board") or "").split("/")[0]
        if tag in boards and board not in (boards[tag].get("zephyr_boards") or []):
            wrong.append(f"class {n}: {board} not in {tag}'s zephyr_boards")
    assert not wrong, "\n".join(wrong)
