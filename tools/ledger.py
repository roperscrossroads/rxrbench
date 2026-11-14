#!/usr/bin/env python3
"""The board exercise's result ledger: what passed on which board type and firmware, against which
code, so a run re-tests only what a change can have affected.

Each step of an exercise depends on the features it exercises on that board (tools/features.py):
the verb's own, the board's family, its flasher, its routes, and the capabilities of the family's
collection that the verb proves. Its fingerprint is a hash of those features' files, of the code
every play stands on (`SHARED_CODE`), and of the board's own inputs (its family, flasher, board
type, routes and declared firmware, from the layout). The site keeps an append-only ledger
(`<site>/exercise/ledger.jsonl`), one line per step that passed or failed:

    {"ts", "run", "board", "board_tag", "fw_id", "verb", "features", "fp", "result", "seconds", "tool_commit"}

A step is fresh when the newest line for (board, verb) passed, has the same board type, firmware
and fingerprint, and is not older than the age limit. A board with no firmware identity is never
fresh. The ledger is private (board names); `tools/matrix.py --public` publishes a summary.
"""
import datetime
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import features  # noqa: E402  pylint: disable=wrong-import-position

C = "collections/ansible_collections/rxrbench"
#: Code under every play: a change here makes every hardware result stale.
SHARED_CODE = (f"{C}/core/plugins/plugin_utils", f"{C}/core/plugins/module_utils/familyspec.py",
               f"{C}/core/plugins/module_utils/site.py", f"{C}/core/plugins/module_utils/flash/common.py",
               f"{C}/core/plugins/module_utils/layout.py", f"{C}/core/plugins/inventory", f"{C}/core/roles/board")
#: The layout values that choose what a step runs on (the board's own inputs to its fingerprint).
BOARD_INPUTS = ("bench_family", "bench_flasher", "bench_board_tag", "bench_routes", "bench_can", "firmware",
                "bench_mcu")
UNKNOWN = "unknown"


def fw_id(hv):
    """The firmware a board is declared to run, as one string, or UNKNOWN: its family and what tells
    its image apart (image, class, version, build). A declaration that names none of those (a bare
    chip, a foreign one) is unknown, and an unknown firmware is never fresh."""
    fw = hv.get("firmware") if isinstance(hv.get("firmware"), dict) else {}
    parts = [f"{k}={fw[k]}" for k in ("image", "class", "version", "build") if fw.get(k) not in (None, "")]
    if not parts:
        return UNKNOWN
    return ";".join([f"family={fw.get('family') or hv.get('bench_family') or '?'}", *parts])


def step_features(hv, verb, feats):
    """The features a verb exercises on this board."""
    fam, flasher = hv.get("bench_family"), hv.get("bench_flasher")
    routes = {r.get("bearer") for r in (hv.get("bench_routes") or {}).values() if isinstance(r, dict)}
    family_coll = next((f["collection"] for f in feats if f["kind"] == "family" and f.get("name") == fam), None)
    out = []
    for f in feats:
        hw = (f.get("tests") or {}).get("hw") or []
        if f["kind"] == "verb" and f.get("name") == verb:
            out.append(f["id"])
        elif f["kind"] == "family" and f.get("name") == fam:
            out.append(f["id"])
        elif f["kind"] == "flasher" and f.get("name") == flasher:
            out.append(f["id"])
        elif f["kind"] == "route" and f.get("name") in routes:
            out.append(f["id"])
        elif f["kind"] == "capability" and verb in hw and f["collection"] in (family_coll, "rxrbench.core"):
            out.append(f["id"])
    return sorted(out)


def _hash_files(h, paths):
    for rel in sorted(paths):
        p = os.path.join(REPO, rel)
        h.update(rel.encode() + b"\0")
        try:
            with open(p, "rb") as fh:
                h.update(hashlib.sha256(fh.read()).digest())
        except OSError:
            h.update(b"missing")


def _shared_files():
    out = set()
    for s in SHARED_CODE:
        p = os.path.join(REPO, s)
        if os.path.isfile(p):
            out.add(s)
        for root, _dirs, files in os.walk(p):
            if "__pycache__" in root:
                continue
            out.update(os.path.relpath(os.path.join(root, f), REPO) for f in files if not f.endswith(".pyc"))
    return out


def fingerprint(hv, feature_ids, feats):
    """A hash of the code the step depends on (its features' files and the shared code, as they are
    on disk) and of the board's own inputs. The same code and board give the same value."""
    by_id = {f["id"]: f for f in feats}
    files = set(_shared_files())
    for fid in feature_ids:
        files |= features.files_of(by_id[fid])
    h = hashlib.sha256()
    _hash_files(h, files)
    h.update(json.dumps({k: hv.get(k) for k in BOARD_INPUTS}, sort_keys=True, default=str).encode())
    return h.hexdigest()[:16]


def path_of(site_dir):
    return os.path.join(site_dir, "exercise", "ledger.jsonl")


def read(path):
    """Every line of a ledger, oldest first ([] when there is none)."""
    out = []
    if not os.path.isfile(path):
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def append(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        for ln in lines:
            fh.write(json.dumps(ln, sort_keys=True) + "\n")


def lines_of(rec):
    """The ledger lines for a record: one per step that passed or failed (v2 records)."""
    out = []
    for s in rec.get("steps") or []:
        if s.get("result") not in ("pass", "fail") or not s.get("fp"):
            continue
        out.append({"ts": rec.get("run"), "run": rec.get("run"), "board": rec.get("board"),
                    "board_tag": rec.get("board_tag"), "fw_id": (rec.get("firmware") or {}).get("id", UNKNOWN),
                    "verb": s["verb"], "features": s.get("features") or [], "fp": s["fp"],
                    "result": s["result"], "seconds": s.get("seconds"), "tool_commit": rec.get("tool_commit")})
    return out


def _when(ts):
    try:
        return datetime.datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def fresh(entries, board, verb, board_tag, fwid, fp, max_age_days=None, now=None):
    """(fresh, the newest line for this board and verb or None). Fresh: it passed, on the same board
    type and firmware, against the same fingerprint, within the age limit; never for UNKNOWN firmware."""
    last = next((e for e in reversed(entries) if e.get("board") == board and e.get("verb") == verb), None)
    if not last or fwid == UNKNOWN:
        return False, last
    ok = (last.get("result") == "pass" and last.get("fp") == fp and last.get("board_tag") == board_tag
          and last.get("fw_id") == fwid)
    if ok and max_age_days is not None:
        when = _when(last.get("ts"))
        now = now or datetime.datetime.now(datetime.timezone.utc)
        ok = bool(when) and (now - when).days <= max_age_days
    return ok, last
