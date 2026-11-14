"""tools/matrix.py: the capability matrix's table is generated from the board exercise's records,
the newest per board, verbs as rows and boards as columns, a cell saying what the record says
(passed with its seconds, failed, not run, does not apply) and a line per board giving the reasons;
the table lives between two markers and the hand-written text around it is kept."""
import importlib.util
import json
import os

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
SPEC = importlib.util.spec_from_file_location("matrix", os.path.join(REPO, "tools", "matrix.py"))
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


def step(verb, kind, result, applies=True, reason="", msg="", seconds=0):
    return {"verb": verb, "kind": kind, "applies": applies, "reason": reason, "result": result,
            "seconds": seconds, "evidence": {}, "msg": msg}


def record(board, run, steps, **more):
    return {"v": 1, "run": run, "layout": "lay", "board": board, "destructive": False, "family": "fam",
            "flasher": "esptool", "routes": ["usb"], "tool_commit": "abc1234", "site_commit": "def5678",
            "seconds": 100.0, "came_back": True, "passed": all(s["result"] != "fail" for s in steps),
            "steps": steps, **more}


ONE = record("node1", "20260101T000000Z", [
    step("plan", "read", "pass", seconds=29.2),
    step("boot", "read", "skip", applies=False, reason="no bootloader declared for it", msg="no bootloader declared for it"),
    step("apply", "reversible", "pass", seconds=4.0),
    step("erase-chip", "destructive", "skip", msg="destructive verbs run only with --destructive"),
    step("came-back", "read", "pass", seconds=30.0),
])
TWO = record("node2", "20260102T000000Z", [
    step("plan", "read", "pass", seconds=10.0),
    step("boot", "read", "pass", seconds=3.0),
    step("apply", "reversible", "fail", msg="changed 2"),
    step("erase-chip", "destructive", "skip", msg="a write before it failed: apply"),
    step("came-back", "read", "fail", msg="the board did not come back as it was"),
], came_back=False)


def write(d, name, rec):
    with open(os.path.join(d, name), "w") as fh:
        json.dump(rec, fh)


def test_the_table_has_a_row_per_verb_and_a_column_per_board_each_cell_from_its_record(tmp_path):
    write(tmp_path, "node1.json", ONE)
    write(tmp_path, "node2.json", TWO)
    out = matrix.render(matrix.load(str(tmp_path)))
    lines = out.splitlines()
    assert lines[0] == "| Verb | Kind | node1 | node2 |"
    assert "| `plan` | read | Yes (29 s) | Yes (10 s) |" in lines
    assert "| `boot` | read | — | Yes (3.0 s) |" in lines
    assert "| `apply` | reversible | Yes (4.0 s) | **No** |" in lines
    assert "| `erase-chip` | destructive | not run | not run |" in lines
    assert "| `came-back` | read | Yes (30 s) | **No** |" in lines


def test_the_line_per_board_gives_the_run_the_commits_and_every_reason(tmp_path):
    write(tmp_path, "node1.json", ONE)
    write(tmp_path, "node2.json", TWO)
    out = matrix.render(matrix.load(str(tmp_path)))
    one = next(l for l in out.splitlines() if l.startswith("- **node1**"))
    assert "layout `lay`, fam over esptool (usb); " + matrix._when(ONE["run"]) + ", 100 s, tool `abc1234`, site `def5678`" in one
    assert matrix._when(ONE["run"]).endswith(" 00:00Z") and matrix._when("odd") == "odd"
    assert "without `--destructive`; came back." in one
    assert "Not run: erase-chip (destructive verbs run only with --destructive)" in one
    assert "Does not apply: boot (no bootloader declared for it)" in one
    two = next(l for l in out.splitlines() if l.startswith("- **node2**"))
    assert "DID NOT COME BACK" in two
    assert "Failed: apply (changed 2); came-back (the board did not come back as it was)" in two
    assert "Not run: erase-chip (a write before it failed: apply)" in two


def test_the_newest_record_per_board_wins_and_the_boards_come_in_the_order_of_their_runs(tmp_path):
    write(tmp_path, "node2.json", TWO)
    write(tmp_path, "node1.json", ONE)
    older = record("node1", "20251201T000000Z", [step("plan", "read", "fail", msg="old")])
    write(tmp_path, "node1-older.json", older)
    write(tmp_path, "notes.json", {"not": "a record"})
    recs = matrix.load(str(tmp_path))
    assert [r["board"] for r in recs] == ["node1", "node2"]
    assert recs[0]["run"] == "20260101T000000Z"


def test_the_table_replaces_what_is_between_the_markers_and_keeps_the_rest():
    text = f"# Matrix\n\nIntro.\n\n{matrix.BEGIN}\nold table\n{matrix.END}\n\n## Notes\n\nkept.\n"
    new = matrix.splice(text, "new table\n")
    assert new == f"# Matrix\n\nIntro.\n\n{matrix.BEGIN}\nnew table\n{matrix.END}\n\n## Notes\n\nkept.\n"
    assert matrix.splice(new, "new table\n") == new


def test_a_file_without_markers_gets_them_before_its_first_heading():
    new = matrix.splice("# Matrix\n\nIntro.\n\n## Notes\n\nkept.\n", "table\n")
    assert new == f"# Matrix\n\nIntro.\n\n{matrix.BEGIN}\ntable\n{matrix.END}\n\n## Notes\n\nkept.\n"


def test_check_says_whether_the_file_is_what_the_records_say(tmp_path, capsys):
    recs = tmp_path / "exercise"
    recs.mkdir()
    write(str(recs), "node1.json", ONE)
    out = tmp_path / "MATRIX.md"
    out.write_text("# Matrix\n\n## Notes\n")
    assert matrix.main(["--records", str(recs), "--out", str(out), "--check"]) == 1
    assert matrix.main(["--records", str(recs), "--out", str(out)]) == 0
    assert matrix.main(["--records", str(recs), "--out", str(out), "--check"]) == 0
    assert matrix.main(["--records", str(recs), "--out", str(out)]) == 0
    assert "unchanged" in capsys.readouterr().out
    text = out.read_text()
    assert text.startswith("# Matrix\n\n" + matrix.BEGIN) and text.endswith("## Notes\n")
    assert "| `plan` | read | Yes (29 s) |" in text


def test_no_record_yet_is_said_in_the_tables_place(tmp_path):
    assert "No exercise record yet" in matrix.render(matrix.load(str(tmp_path)))


# ---- the ledger: by board type and firmware, and the public table ------------------------------

LEDGER = [
    {"ts": "20261114T100000Z", "board": "b1", "board_tag": "type-a", "fw_id": "family=fam;image=fam-a;version=1.2",
     "verb": "apply", "result": "fail", "tool_commit": "aaa"},
    {"ts": "20261114T120000Z", "board": "b1", "board_tag": "type-a", "fw_id": "family=fam;image=fam-a;version=1.2",
     "verb": "apply", "result": "pass", "tool_commit": "bbb"},
    {"ts": "20261114T120000Z", "board": "b2", "board_tag": "type-b", "fw_id": "family=other;build=deadbeef",
     "verb": "apply", "result": "fail", "tool_commit": "bbb"},
]


def test_the_ledger_table_is_by_board_type_and_firmware_with_the_newest_result():
    body = matrix.render_ledger(LEDGER)
    assert "type-a / fam image fam-a version 1.2 (b1)" in body and "type-b / other build deadbeef (b2)" in body
    row = next(ln for ln in body.splitlines() if ln.startswith("| `apply`"))
    day = matrix._when(LEDGER[1]["ts"])[:10]                  # pylint: disable=protected-access
    assert f"Yes {day} `bbb`" in row and f"**No** {day} `bbb`" in row and "aaa" not in row


def test_the_public_table_names_no_board_image_or_build():
    body = matrix.render_ledger(LEDGER, public=True)
    assert "b1" not in body.replace("bbb", "") and "fam-a" not in body and "deadbeef" not in body
    assert "type-a / fam version 1.2" in body and "type-b / other" in body


def test_the_public_table_is_written_only_past_the_privacy_check(tmp_path):
    ok = tmp_path / "ok.md"
    assert matrix.write_public(str(ok), matrix.render_ledger(LEDGER, public=True)) == 0 and ok.exists()
    leaky = [{**LEDGER[0], "board_tag": "bench1." + ".".join(["home", "arpa"])}]   # built here: the tree holds no such name
    bad = tmp_path / "bad.md"
    assert matrix.write_public(str(bad), matrix.render_ledger(leaky, public=True)) == 1 and not bad.exists()


def test_a_v2_record_is_read_like_a_v1_record(tmp_path):
    (tmp_path / "b1.json").write_text(json.dumps({"v": 2, "run": "20261114T120000Z", "board": "b1",
                                                  "steps": [], "board_tag": "type-a"}))
    assert [r["board"] for r in matrix.load(str(tmp_path))] == ["b1"]
