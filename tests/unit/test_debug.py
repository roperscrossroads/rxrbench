"""just debug: unwrapping the inventory, and which symbols go with what a board runs."""
import importlib.util
import json
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("debug_tool", os.path.join(REPO, "tools", "debug.py"))
debug = importlib.util.module_from_spec(spec)
spec.loader.exec_module(debug)


def test_inventory_values_come_out_of_their_wrapper():
    assert debug.plain({"ansible_user": {"__ansible_unsafe": "bench"}, "n": [{"__ansible_unsafe": "x"}]}) \
        == {"ansible_user": "bench", "n": ["x"]}


@pytest.mark.plugins
def test_the_symbols_are_those_of_the_running_build_class_and_brainness(tmp_path, monkeypatch):
    monkeypatch.setattr(debug, "REPO", str(tmp_path))
    for key, cls, feats in (("class-5-abc", 5, []), ("class-4-abc", 4, []),
                            ("class-4-brain-abc", 4, ["brain"])):
        store = tmp_path / ".build" / "store" / "zephyrtastic" / key
        store.mkdir(parents=True)
        (store / "manifest.json").write_text(json.dumps({"build": "abc", "class": cls,
                                                         "features": feats}))
        dbg = tmp_path / ".build" / "debug" / "zephyrtastic" / key
        dbg.mkdir(parents=True)
        (dbg / "zephyr.elf").write_bytes(b"\x7fELF")
    elf, build = debug.symbols_for("zephyrtastic", {"build": "abc", "class": 4, "brain": {}})
    assert build == "abc" and elf.endswith("class-4-brain-abc/zephyr.elf")
    elf, _ = debug.symbols_for("zephyrtastic", {"build": "abc", "class": 4})
    assert elf.endswith("class-4-abc/zephyr.elf")
    assert debug.symbols_for("zephyrtastic", {"build": "zzz", "class": 4}) == (None, None)
    # the family's identity key (here `class`) tells images of one build apart
    elf, _ = debug.symbols_for("zephyrtastic", {"build": "abc", "class": 5})
    assert elf.endswith("class-5-abc/zephyr.elf")


def test_the_symbols_are_the_one_elf_staged_beside_the_image_whatever_its_name(tmp_path, monkeypatch):
    monkeypatch.setattr(debug, "REPO", str(tmp_path))
    store = tmp_path / ".build" / "store" / "bare" / "img"
    store.mkdir(parents=True)
    (store / "manifest.json").write_text(json.dumps({"build": "b1"}))
    dbg = tmp_path / ".build" / "debug" / "bare" / "img"
    dbg.mkdir(parents=True)
    (dbg / "app.elf").write_bytes(b"\x7fELF")
    assert debug.symbols_for("bare", {"build": "b1"}) == (str(dbg / "app.elf"), "b1")
    (dbg / "other.elf").write_bytes(b"\x7fELF")
    assert debug.symbols_for("bare", {"build": "b1"}) == (None, None), "two ELF files: which is it?"
