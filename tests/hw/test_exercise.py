"""The board exercise on real boards (tests/hw/README.md, "The board exercise"): tools/exercise.py
runs once per board named in RXRBENCH_HW_BOARDS, and every verb of it is one test here, so a
verb that does not apply is a skip with its reason and one verb can be asked for with -k. The
exercise writes to the board: every reversible verb, and with RXRBENCH_HW_DESTRUCTIVE=1 the
destructive ones too. With RXRBENCH_HW_RECORD=1 the record is kept as the site's latest for the
board (`<site>/exercise/<board>.json`), the file tools/matrix.py reads."""
import importlib.util
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEC = importlib.util.spec_from_file_location("exercise", os.path.join(REPO, "tools", "exercise.py"))
exercise = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exercise)

BOARDS = [b for b in os.environ.get("RXRBENCH_HW_BOARDS", "").split(",") if b]
LAYOUT = os.environ.get("RXRBENCH_HW_LAYOUT", "")
DESTRUCTIVE = bool(os.environ.get("RXRBENCH_HW_DESTRUCTIVE"))
SKIP = [s for s in os.environ.get("RXRBENCH_HW_SKIP", "").split(",") if s]
RECORD = bool(os.environ.get("RXRBENCH_HW_RECORD"))

pytestmark = pytest.mark.skipif(not BOARDS or not LAYOUT,
                                reason="no board named in RXRBENCH_HW_BOARDS, or no layout in RXRBENCH_HW_LAYOUT")

_records = {}


def present(board):
    r = subprocess.run(["devreg", "resolve", board], capture_output=True, text=True, check=False)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def record(board):
    """The board's exercise record, run once for every test of it."""
    if board not in _records:
        ok, said = present(board)
        if not ok:
            pytest.skip(f"{board}: {said}")
        exercise.site.load_env()
        _records[board] = exercise.Exercise(LAYOUT, board, destructive=DESTRUCTIVE, skip=SKIP).go()
        if RECORD:
            _records[board]["kept"] = exercise.keep(_records[board])
    return _records[board]


@pytest.mark.parametrize("board", BOARDS)
@pytest.mark.parametrize("verb", exercise.VERBS + ["came-back"])
def test_verb(board, verb):
    rec = record(board)
    step = next(s for s in rec["steps"] if s["verb"] == verb)
    if step["result"] == exercise.SKIP:
        pytest.skip(f"{board} {verb}: {step['msg']}")
    assert step["result"] == exercise.PASS, f"{board} {verb}: {step['msg']} ({step['evidence']}); record {rec['path']}"


@pytest.mark.parametrize("board", BOARDS)
def test_the_record_is_written(board):
    rec = record(board)
    assert os.path.isfile(rec["path"]) and rec["board"] == board and rec["layout"] == LAYOUT
