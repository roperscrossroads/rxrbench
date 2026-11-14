"""tools/exercise.py: which verbs of the board exercise apply to a board is derived from what the
inventory publishes for it and from the catalog, with a reason for each verb that does not; a
board its layout does not let the tool write gets the reads alone. The runner takes the verbs in
order, reads first, each write with its undo, destructive ones only when asked, stops the writes
after a failed one or a start that was not clean, and always plans the board at the end."""
import importlib.util
import json
import os

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
SPEC = importlib.util.spec_from_file_location("exercise", os.path.join(REPO, "tools", "exercise.py"))
exercise = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exercise)

#: A console board with a flasher that reads regions and the chip, a device-made secret, tests,
#: a bootloader and two images of its family and one of another in the catalog.
FULL = {
    "bench_family": "fam", "bench_via": "console", "bench_vocabulary": "fam", "bench_plugin": "acme.widgets",
    "bench_flasher": "esptool",
    "bench_can": ["regions", "chip"], "bench_apply_allowed": True, "bench_locate": "shell",
    "bench_board_tag": "type-a", "bench_bootloader": {"name": "boot-1"}, "bench_access_host": "host1",
    "bench_tests": [{"name": "version", "cmd": "version", "expect": "^v"}],
    "bench_secret_kinds": [{"kind": "identity", "origin": "device", "escrow": True},
                           {"kind": "channel_key", "origin": "declared", "escrow": False}],
    "bench_routes": {"shell": {"bearer": "usb"}, "flash": {"bearer": "usb"}},
    "bench_families": {"fam": {"console": "shell", "regions": [["storage", "STORAGE"]],
                               "reboot": {"reboot": "reboot"}}},
    "firmware": {"family": "fam", "image": "fam-a"},
}
IMAGES = {
    "fam-a": {"family": "fam", "board_tags": ["type-a"]},
    "fam-b": {"family": "fam", "board_tags": ["type-a"]},
    "other-1": {"family": "other", "board_tags": ["type-a"]},
    "elsewhere": {"family": "fam", "board_tags": ["type-b"]},
}
#: The site's alternative declaration of FULL's board as the other family.
ALTS = [{"name": "as-other", "path": "alternatives/b1/as-other.yml",
         "vars": {"firmware": {"family": "other", "image": "other-1"}, "other": {"name": "drill"}, "bench_first_flash": True}}]
#: A simulated board known by its bytes alone: a flash file, no console, no secrets.
SIM = {
    "bench_family": "bare", "bench_via": None, "bench_vocabulary": None, "bench_can": ["chip"],
    "bench_flasher": "sim", "bench_apply_allowed": True, "bench_locate": None, "bench_board_tag": "sim-type",
    "bench_bootloader": None, "bench_tests": [], "bench_secret_kinds": [], "bench_access_host": "simhost",
    "bench_routes": {"flash": {"bearer": "sim"}}, "bench_families": {"bare": {}},
    "firmware": {"family": "bare", "image": "img-1"},
}


def by_verb(verbs):
    return {v["verb"]: v for v in verbs}


# ---- what applies -----------------------------------------------------------------------------

def test_every_verb_applies_to_a_board_that_has_everything():
    v = by_verb(exercise.applicable(FULL, IMAGES, alts=ALTS))
    not_applying = sorted(name for name, x in v.items() if not x["applies"] and x["kind"] != exercise.NOT_RUN)
    assert not_applying == []
    assert v["update-there-and-back"]["reason"].startswith("a second image")
    assert "`as-other` declares other" in v["family-switch"]["reason"]


def test_a_family_switch_needs_the_sites_declaration_of_the_other_family_not_its_image_alone():
    """Another family's image for the board type is not enough: its configuration (the family's
    block, the go-aheads) comes from the site's alternatives/<board>/<name>.yml."""
    v = by_verb(exercise.applicable(FULL, IMAGES))
    assert not v["family-switch"]["applies"]
    assert v["family-switch"]["reason"].startswith("no alternative declares another family") and "other" in v["family-switch"]["reason"]
    same = [{"name": "same", "path": "x", "vars": {"firmware": {"family": "fam", "image": "fam-b"}}}]
    assert not by_verb(exercise.applicable(FULL, IMAGES, alts=same))["family-switch"]["applies"]
    elsewhere = [{"name": "nope", "path": "x", "vars": {"firmware": {"family": "other", "image": "not-catalogued"}}}]
    assert not by_verb(exercise.applicable(FULL, IMAGES, alts=elsewhere))["family-switch"]["applies"]
    assert exercise.other_family(FULL, IMAGES, same + elsewhere + ALTS)["name"] == "as-other"


def test_the_alternatives_are_read_from_the_site_by_board(tmp_path):
    d = tmp_path / "alternatives" / "b1"
    d.mkdir(parents=True)
    (d / "zz.yml").write_text("firmware: {family: other, image: other-1}\nother: {name: x}\n")
    (d / "aa.yml").write_text("# a comment only\n")
    (d / "mm.yml").write_text("firmware: {family: fam, class: 5}\n")
    alts = exercise.alternatives(str(tmp_path), "b1")
    assert [a["name"] for a in alts] == ["mm", "zz"] and alts[1]["vars"]["other"] == {"name": "x"}
    assert exercise.alternatives(str(tmp_path), "b2") == [] and exercise.alternatives(None, "b1") == []


def test_the_two_verbs_the_exercise_never_runs_say_why():
    v = by_verb(exercise.applicable(FULL, IMAGES))
    for name in ("first-flash", "card"):
        assert v[name]["kind"] == exercise.NOT_RUN and not v[name]["applies"] and v[name]["reason"]


def test_a_board_known_by_its_bytes_gets_the_chip_verbs_and_a_reason_for_each_other():
    v = by_verb(exercise.applicable(SIM, {"img-1": {"family": "bare", "board_tags": ["sim-type"]},
                                          "img-2": {"family": "bare", "board_tags": ["sim-type"]}}))
    assert {n for n, x in v.items() if x["applies"]} == {
        "plan", "backup-chip", "apply", "snapshot-switch", "update-there-and-back", "erase-chip"}
    assert v["facts"]["reason"] == "the family has no facts step: no vocabulary"
    assert v["backup-regions"]["reason"] == "the flasher cannot read regions"
    assert v["keys-export"]["reason"] == "no secret the device makes"
    assert v["family-switch"]["reason"].startswith("no alternative declares another family")
    assert v["survey-deep"]["reason"] == "no USB route"


def test_a_board_the_layout_does_not_let_the_tool_write_gets_the_reads_alone():
    hv = {**FULL, "bench_apply_allowed": False}
    v = by_verb(exercise.applicable(hv, IMAGES))
    applying = {n for n, x in v.items() if x["applies"]}
    assert applying == {"survey", "facts", "plan", "secrets", "diag", "device-test", "locate", "boot",
                        "backup-regions", "backup-chip", "keys-export"}
    assert v["apply"]["reason"] == "the layout gives this board no bench_apply_allowed"
    assert v["erase-chip"]["reason"] == v["apply"]["reason"] == v["survey-deep"]["reason"]


def test_a_flasher_that_reads_nothing_back_leaves_the_backups_out_with_the_reason():
    hv = {**FULL, "bench_can": [], "bench_flasher": "uf2",
          "bench_families": {"fam": {"console": "shell", "reboot": {"reboot": "reboot"}}}}
    v = by_verb(exercise.applicable(hv, IMAGES))
    assert not v["backup-regions"]["applies"] and v["backup-regions"]["reason"] == "the flasher cannot read regions"
    assert not v["backup-chip"]["applies"] and "cannot read the whole chip" in v["backup-chip"]["reason"]
    assert not v["erase-chip"]["applies"] and not v["snapshot-switch"]["applies"]
    assert not v["survey-deep"]["applies"] and "ROM loader" in v["survey-deep"]["reason"]
    assert v["reboot"]["applies"] and v["keys-export"]["applies"]


def test_a_declared_config_range_gives_the_region_verbs_to_a_family_that_names_none():
    hv = {**FULL, "bench_families": {"fam": {"console": "shell"}},
          "bench_flash_regions": [{"label": "cfg", "role": "config"}, {"label": "app", "role": "app"}]}
    v = by_verb(exercise.applicable(hv, IMAGES))
    assert v["backup-regions"]["applies"] and v["erase-region"]["applies"]
    hv["bench_flash_regions"] = [{"label": "app", "role": "app"}]
    v = by_verb(exercise.applicable(hv, IMAGES))
    assert not v["backup-regions"]["applies"] and "no config regions" in v["backup-regions"]["reason"]


def test_a_family_built_here_updates_to_another_staged_class_with_the_same_features():
    hv = {**FULL, "firmware": {"family": "fam", "class": 4, "features": ["x"]}}
    staged = {"c4": {"family": "fam", "board_tag": "type-a", "class": 4, "features": ["x"], "staged": True},
              "c6": {"family": "fam", "board_tag": "type-a", "class": 6, "features": ["x"], "staged": True},
              "c7": {"family": "fam", "board_tag": "type-a", "class": 7, "features": [], "staged": True}}
    assert exercise.other_image(hv, staged) == {"family": "fam", "class": 6, "features": ["x"]}
    v = by_verb(exercise.applicable(hv, staged))
    assert v["update-there-and-back"]["applies"] and "class" in v["update-there-and-back"]["reason"]
    del staged["c6"]
    assert exercise.other_image(hv, staged) is None, "another class with other features is not the same board"


def test_the_order_is_reads_then_reversible_then_destructive():
    kinds = [v["kind"] for v in exercise.applicable(FULL, IMAGES)]
    order = [exercise.READ, exercise.REVERSIBLE, exercise.DESTRUCTIVE, exercise.NOT_RUN]
    assert kinds == sorted(kinds, key=order.index)
    verbs = [v["verb"] for v in exercise.applicable(FULL, IMAGES)]
    assert verbs.index("backup-chip") < verbs.index("survey-deep") < verbs.index("apply"), "the deep survey after the backups"


def test_staged_builds_count_as_images_of_their_family_for_their_board_type(tmp_path):
    store = tmp_path / "store" / "zephyr"
    for name, fam in (("app-1", "fam"), ("app-2", "fam"), ("other-app", "other"), ("elsewhere-app", "fam")):
        d = store / name
        d.mkdir(parents=True)
        tag = "type-b" if name.startswith("elsewhere") else "type-a"
        (d / "manifest.json").write_text('{"family": "%s", "board_tag": "%s", "build": "abc"}' % (fam, tag))
    (store / "broken").mkdir()
    (store / "broken" / "manifest.json").write_text("not json")
    images = exercise.staged_images(str(tmp_path / "store"))
    assert set(images) == {"app-1", "app-2", "other-app", "elsewhere-app"}
    hv = {**SIM, "bench_board_tag": "type-a", "bench_family": "fam", "firmware": {"family": "fam", "image": "app-1"}}
    v = by_verb(exercise.applicable(hv, images))
    assert v["update-there-and-back"]["applies"] and not v["family-switch"]["applies"]
    alts = [{"name": "o", "path": "x", "vars": {"firmware": {"family": "other", "image": "other-app"}}}]
    assert by_verb(exercise.applicable(hv, images, alts=alts))["family-switch"]["applies"]
    assert by_verb(exercise.applicable(hv, {"app-1": images["app-1"]}))["update-there-and-back"]["applies"] is False


# ---- the runner, against fake plays ----------------------------------------------------------

REGIONS_STAMP, CHIP_STAMP = "20261114T130000Z", "20261114T130001Z"


class Plays:
    """Stands in for ansible-playbook: records every play's spec, answers as told."""

    def __init__(self, fail=(), changes=None, dirty_start=False, dirty_end=False):
        self.specs, self.fail, self.changes, self.dirty_start = [], set(fail), changes or {}, dirty_start
        self.dirty_end = dirty_end

    def __call__(self, spec):
        self.specs.append(spec)
        name = spec["run"].rsplit("/", 1)[1][3:]            # "<nn>-<verb>" -> "<verb>"
        rc = 2 if name in self.fail else 0
        recap = {"ok": 3, "changed": self.changes.get(name, 0), "unreachable": 0, "failed": 1 if rc else 0, "skipped": 0}
        state = None
        if spec["state"]:
            dirty = (self.dirty_start and name == "plan") or (self.dirty_end and name == "plan-end")
            state = {"boards": {spec["board"]: {"changes": 2 if dirty else 0, "answered": True, "failed": False,
                                                "reported": {"family": "fam", "class": 4}}}}
        out = ""
        if name == "backup" and not rc:        # the play's "Where it went" line names its backups
            out = f"{spec['board']}: regions {REGIONS_STAMP} (storage); " + \
                ("" if (spec["extra"] or {}).get("chip") is False else f"chip {CHIP_STAMP} (02:00:00:00:00:01); ") + \
                f"copies in backups/{spec['board']}/"
        return {"rc": rc, "out": out, "recap": recap, "state": state}

    def names(self):
        return [s["run"].rsplit("/", 1)[1][3:] for s in self.specs]


def make(tmp_path, plays, destructive=False, hv=None, skip=(), images=None, alts=()):
    return exercise.Exercise("lay", "b1", destructive=destructive, skip=skip, runs=str(tmp_path), now="T",
                             plays=plays, hv=hv or FULL, images=IMAGES if images is None else images,
                             layout_dir=str(tmp_path / "lay"), hosts_dir=str(tmp_path / "hosts"), alts=alts)


def states_in_place(monkeypatch):
    """campaign.make_state without a site: the state copy is its directory, and the override is kept."""
    import campaign  # pylint: disable=import-outside-toplevel
    made = []

    def make_state(layout_dir, st, where):
        made.append((layout_dir, st, where))
        return where
    monkeypatch.setattr(campaign, "make_state", make_state)
    return made


def test_the_runner_takes_the_verbs_in_order_each_write_with_its_undo_and_plans_the_board_last(tmp_path):
    (tmp_path / "lay").mkdir()
    plays = Plays()
    ex = make(tmp_path, plays)
    ex.v_update_there_and_back = lambda: (exercise.PASS, {"there": "fam-b"}, "")   # the state copy needs a real site
    rec = ex.go()
    assert rec["passed"] and rec["came_back"], rec["steps"]
    assert plays.names() == [
        "survey", "facts", "plan", "secrets", "diag", "device-test", "locate", "boot",
        "reboot", "backup", "survey-deep", "keys-export", "apply",
        "restore-regions", "plan-after-restore-regions", "snapshot", "switch", "plan-after-switch",
        "keys-restore", "plan-end"]
    results = {s["verb"]: s["result"] for s in rec["steps"]}
    assert results["backup-regions"] == results["backup-chip"] == exercise.PASS, "one backup play, both levels"
    assert results["erase-region"] == exercise.SKIP and "--destructive" in next(
        s["msg"] for s in rec["steps"] if s["verb"] == "erase-region")
    assert results["family-switch"] == exercise.SKIP and results["came-back"] == exercise.PASS
    with open(rec["path"]) as f:
        assert json.load(f)["run"] == "T"
    survey = plays.specs[0]
    assert survey["inventory"].endswith("hosts") and survey["args"] == ["--limit", "host1"] and survey["extra"] == {"survey_deep": False}
    assert all("--limit" in s["args"] and "b1" in s["args"] for s in plays.specs if s["playbook"] != "survey.yml")
    assert [s["state"] for s in plays.specs if s["playbook"] == "apply.yml" and "--check" in s["args"]] == [True] * 4


def test_destructive_verbs_run_only_when_asked_each_followed_by_its_restore_and_a_plan(tmp_path):
    (tmp_path / "lay").mkdir()
    plays = Plays()
    ex = make(tmp_path, plays, destructive=True)
    ex.v_update_there_and_back = lambda: (exercise.PASS, {}, "")
    rec = ex.go()
    names = plays.names()
    i = names.index("erase-region")
    assert names[i:i + 5] == ["erase-region", "apply-after-erase", "keys-restore-after-erase", "plan-after-erase", "secrets-after-erase"]
    j = names.index("erase-chip")
    assert names[j:j + 3] == ["erase-chip", "restore-chip", "plan-after-restore-chip"]
    assert names[-1] == "plan-end" and rec["passed"]
    erase = plays.specs[i]
    assert erase["extra"] == {"erase_region": "storage"} and "--check" not in erase["args"]


def test_a_start_that_is_not_clean_skips_every_write_and_still_runs_the_reads_and_the_final_plan(tmp_path):
    (tmp_path / "lay").mkdir()
    plays = Plays(dirty_start=True)
    rec = make(tmp_path, plays, destructive=True).go()
    results = {s["verb"]: s for s in rec["steps"]}
    assert results["plan"]["result"] == exercise.FAIL and "2 changes" in results["plan"]["msg"]
    for v in ("reboot", "apply", "snapshot-switch", "erase-chip", "survey-deep"):
        assert results[v]["result"] == exercise.SKIP and results[v]["msg"].startswith("writes skipped: the board did not match")
    for v in ("facts", "secrets", "diag", "locate", "boot", "backup-regions", "keys-export"):
        assert results[v]["result"] == exercise.PASS, v
    assert plays.names()[-1] == "plan-end" and not rec["passed"]


def test_a_failed_write_stops_the_writes_after_it_but_not_the_final_plan(tmp_path):
    (tmp_path / "lay").mkdir()
    plays = Plays(fail=["snapshot"])
    ex = make(tmp_path, plays, destructive=True)
    ex.v_update_there_and_back = lambda: (exercise.PASS, {}, "")
    rec = ex.go()
    results = {s["verb"]: s for s in rec["steps"]}
    assert results["snapshot-switch"]["result"] == exercise.FAIL and results["snapshot-switch"]["msg"] == "the snapshot failed"
    for v in ("update-there-and-back", "keys-restore", "erase-region", "erase-chip"):
        assert results[v]["result"] == exercise.SKIP and "after a failed one: snapshot-switch" in results[v]["msg"], v
    assert "switch" not in plays.names() and plays.names()[-1] == "plan-end"
    assert not rec["passed"] and rec["came_back"]


def test_an_apply_that_changes_something_fails_the_verb_and_a_skipped_verb_is_said(tmp_path):
    (tmp_path / "lay").mkdir()
    plays = Plays(changes={"apply": 1})
    ex = make(tmp_path, plays, skip=["survey", "survey-deep"])
    ex.v_update_there_and_back = lambda: (exercise.PASS, {}, "")
    rec = ex.go()
    results = {s["verb"]: s for s in rec["steps"]}
    assert results["apply"]["result"] == exercise.FAIL and "1 tasks changed" in results["apply"]["msg"]
    assert results["survey"]["result"] == exercise.SKIP and results["survey"]["msg"] == "skipped on request"
    assert "survey" not in plays.names() and "survey-deep" not in plays.names()


def test_a_read_only_board_runs_its_reads_and_the_backups_and_nothing_that_writes(tmp_path):
    (tmp_path / "lay").mkdir()
    plays = Plays()
    rec = make(tmp_path, plays, destructive=True, hv={**FULL, "bench_apply_allowed": False}).go()
    assert set(plays.names()) == {"survey", "facts", "plan", "secrets", "diag", "device-test", "locate", "boot",
                                  "backup", "keys-export", "plan-end"}
    assert rec["passed"] and rec["came_back"]


def test_a_build_staged_from_the_same_application_is_a_second_image_whatever_family_its_build_recorded():
    staged = {"app-1.0": {"family": "bare", "board_tag": "sim-type", "app": "/src/app", "staged": True},
              "app-1.1": {"family": "bare", "board_tag": "sim-type", "app": "/src/app", "staged": True},
              "other-1.0": {"family": "bare", "board_tag": "sim-type", "app": "/src/other", "staged": True}}
    hv = {**SIM, "bench_family": "fam", "firmware": {"family": "fam", "image": "app-1.0"}}
    assert exercise.other_image(hv, staged) == {"family": "fam", "image": "app-1.1"}
    hv["firmware"] = {"family": "fam", "image": "other-1.0"}
    assert exercise.other_image(hv, staged) is None, "another application is not a second image of this one"
    assert exercise.other_image(hv, staged, accept=lambda fw: fw["image"] == "app-1.1") == {"family": "fam", "image": "app-1.1"}


def test_a_play_runs_with_the_pinned_tools_after_the_users_own_path_as_the_recipes_do():
    env = exercise.play_env({"state": True, "env": {"X": "1"}}, environ={"PATH": "/usr/bin", "HOME": "/home/bench"})
    assert env["PATH"] == "/usr/bin:/home/bench/.local/share/rxrbench/bin" and env["X"] == "1"
    assert env["ANSIBLE_FORCE_COLOR"] == "0" and "rxrbench.core.state" in env["ANSIBLE_CALLBACKS_ENABLED"]
    again = exercise.play_env({"state": False}, environ=env)
    assert again["PATH"] == env["PATH"] and "ANSIBLE_CALLBACKS_ENABLED" not in again or again.get("ANSIBLE_CALLBACKS_ENABLED") == env["ANSIBLE_CALLBACKS_ENABLED"]


def test_a_plan_that_fails_on_the_board_is_said_so_and_skips_the_writes(tmp_path):
    (tmp_path / "lay").mkdir()

    class Failing(Plays):
        def __call__(self, spec):
            r = super().__call__(spec)
            if r["state"]:
                r["state"]["boards"]["b1"]["failed"] = True
            return r
    rec = make(tmp_path, Failing()).go()
    results = {s["verb"]: s for s in rec["steps"]}
    assert results["plan"]["msg"].startswith("the plan failed on the board")
    assert results["apply"]["result"] == exercise.SKIP and "the plan failed" in results["apply"]["msg"]


# ---- templated declarations ---------------------------------------------------------------------

def test_a_templated_declaration_is_rendered_before_the_reader_judges_it():
    """A layout may declare `bench_tests` as a template that is empty for some families;
    `ansible-inventory --host` hands the template over as a string, which would read as "tests
    declared" (seen on a stock-firmware board whose tests are declared for another family)."""
    hv = {**FULL, "bench_tests": "{{ fam_tests if firmware.family == 'fam' else [] }}"}
    asked = []

    def fetch(key):
        asked.append(key)
        return []
    out = exercise.rendered(hv, fetch)
    assert asked == ["bench_tests"] and out["bench_tests"] == []
    assert not by_verb(exercise.applicable(out, IMAGES))["device-test"]["applies"]
    assert exercise.rendered(FULL, fetch) == FULL and asked == ["bench_tests"]  # a list is not asked about


def test_debugs_answer_is_read_for_the_one_variable():
    out = 'node1 | SUCCESS => {\n    "bench_tests": []\n}\n'
    assert exercise.parse_debug(out, "bench_tests") == []
    assert exercise.parse_debug('node1 | SUCCESS => {"bench_tests": [{"name": "v"}]}', "bench_tests") == [{"name": "v"}]
    assert exercise.parse_debug("node1 | UNREACHABLE!", "bench_tests") is None
    assert exercise.parse_debug("node1 | SUCCESS => not json", "bench_tests") is None


# ---- the family switch -------------------------------------------------------------------------

def test_the_family_switch_applies_the_alternative_plans_it_clean_and_comes_back_by_apply(tmp_path, monkeypatch):
    (tmp_path / "lay").mkdir()
    made = states_in_place(monkeypatch)
    plays = Plays()
    ex = make(tmp_path, plays, destructive=True, alts=ALTS)
    ex.v_update_there_and_back = lambda: (exercise.PASS, {}, "")
    rec = ex.go()
    names = plays.names()
    i = names.index("hosts-there")
    assert names[i:i + 8] == ["hosts-there", "switch-there", "plan-switched", "hosts-back", "switch-back",
                              "keys-restore-after-switch", "plan-switched-back", "secrets-after-switch"]
    step = next(s for s in rec["steps"] if s["verb"] == "family-switch")
    assert step["result"] == exercise.PASS and step["evidence"]["back_by"] == "apply"
    assert step["evidence"]["alternative"] == "as-other" and step["evidence"]["family"] == "other"
    assert made[-1][1] == {"boards": {"b1": ALTS[0]["vars"]}} and made[-1][2].endswith("state-alt")
    hosts_there, there, hosts_back, back = plays.specs[i], plays.specs[i + 1], plays.specs[i + 3], plays.specs[i + 4]
    assert there["inventory"].endswith("state-alt") and back["inventory"] == str(tmp_path / "lay")
    # the bench host is set up for each declaration, limited to this board's host, through the
    # host play (a family with a console is written through a broker only that play starts)
    assert hosts_there["playbook"] == "host.yml" and hosts_there["inventory"].endswith("state-alt")
    assert hosts_there["args"] == ["--limit", "host1"] and hosts_back["args"] == ["--limit", "host1"]
    assert hosts_back["inventory"] == str(tmp_path / "lay")
    assert rec["passed"]


def test_a_switch_back_that_fails_falls_back_to_restore_chip_from_this_runs_backup(tmp_path, monkeypatch):
    (tmp_path / "lay").mkdir()
    states_in_place(monkeypatch)
    plays = Plays(fail=["switch-back"])
    ex = make(tmp_path, plays, destructive=True, alts=ALTS)
    ex.v_update_there_and_back = lambda: (exercise.PASS, {}, "")
    rec = ex.go()
    names = plays.names()
    i = names.index("switch-back")
    assert names[i - 1:i + 4] == ["hosts-back", "switch-back", "restore-chip-after-switch", "keys-restore-after-switch", "plan-switched-back"]
    step = next(s for s in rec["steps"] if s["verb"] == "family-switch")
    assert step["result"] == exercise.PASS and step["evidence"]["back_by"] == "restore-chip"
    assert plays.specs[i + 1]["extra"] == {"restore_from": CHIP_STAMP}, "this run's backup, never the latest"


def test_a_switch_that_fails_still_comes_back_and_is_recorded_as_failed(tmp_path, monkeypatch):
    (tmp_path / "lay").mkdir()
    states_in_place(monkeypatch)
    plays = Plays(fail=["switch-there"])
    ex = make(tmp_path, plays, destructive=True, alts=ALTS)
    ex.v_update_there_and_back = lambda: (exercise.PASS, {}, "")
    rec = ex.go()
    names = plays.names()
    i = names.index("switch-there")
    assert names[i:i + 3] == ["switch-there", "hosts-back", "switch-back"]
    step = next(s for s in rec["steps"] if s["verb"] == "family-switch")
    assert step["result"] == exercise.FAIL and step["msg"] == "the switch to the other family failed"
    assert names[-1] == "plan-end" and not rec["passed"]


def test_a_bare_family_with_a_serial_port_gets_no_diag_its_port_has_no_broker():
    """A board whose route is a serial port is reached `via: console`, but a family the tool has
    no console for (bare: a factory image) gets no broker (the layout's no_console), so diag has
    nothing to ask (seen on hardware: the play failed on the missing socket)."""
    hv = {**FULL, "bench_family": "bare", "bench_vocabulary": None, "bench_plugin": None,
          "bench_families": {"bare": {}}, "firmware": {"family": "bare", "image": "fam-a"}}
    v = by_verb(exercise.applicable(hv, IMAGES))
    assert not v["diag"]["applies"] and v["diag"]["reason"].startswith("the family has no console this tool speaks")
    assert not v["reboot"]["applies"]
    assert by_verb(exercise.applicable(FULL, IMAGES))["diag"]["applies"]
    ssh = {**FULL, "bench_via": "ssh", "bench_families": {"fam": {}}}
    assert by_verb(exercise.applicable(ssh, IMAGES))["diag"]["reason"] == "diagnostics through its ssh"


def test_a_family_whose_console_the_layout_names_gets_diag():
    """zephyr_mcumgr's row names no console; a layout gives one board its shell
    (`bench_console: zephyr-shell`) and the board gets a broker, so diag applies (seen on hardware:
    it was skipped as having no broker while one ran)."""
    base = {**FULL, "bench_family": "zephyr_mcumgr", "bench_vocabulary": None, "bench_plugin": None,
            "bench_families": {"zephyr_mcumgr": {}}, "firmware": {"family": "zephyr_mcumgr", "image": "fam-a"}}
    assert not by_verb(exercise.applicable(base, IMAGES))["diag"]["applies"]
    shell = {**base, "bench_console": "zephyr-shell"}
    assert by_verb(exercise.applicable(shell, IMAGES))["diag"]["applies"]


# ---- what a write is undone with: this run's own backups, never the newest one ----------------

def test_restores_write_back_this_runs_backups_by_stamp(tmp_path):
    (tmp_path / "lay").mkdir()
    plays = Plays()
    ex = make(tmp_path, plays, destructive=True)
    ex.v_update_there_and_back = lambda: (exercise.PASS, {}, "")
    rec = ex.go()
    by_name = {p["run"].rsplit("/", 1)[1][3:]: p for p in plays.specs}
    assert by_name["restore-regions"]["extra"] == {"restore_from": REGIONS_STAMP}
    assert by_name["restore-chip"]["extra"] == {"restore_from": CHIP_STAMP}
    steps = {s["verb"]: s for s in rec["steps"]}
    assert steps["backup-chip"]["evidence"]["backup"] == CHIP_STAMP
    assert steps["erase-chip"]["evidence"]["backup"] == CHIP_STAMP


def test_with_the_backups_skipped_nothing_is_erased_or_written_back(tmp_path):
    """--skip on the backups must not let a restore write an older backup over the board."""
    (tmp_path / "lay").mkdir()
    plays = Plays()
    ex = make(tmp_path, plays, destructive=True, skip=("backup-regions", "backup-chip"), alts=ALTS)
    ex.v_update_there_and_back = lambda: (exercise.PASS, {}, "")
    rec = ex.go()
    names = plays.names()
    assert "backup" not in names
    for play in ("restore-regions", "erase-region", "erase-chip", "restore-chip", "switch-there"):
        assert play not in names, play
    steps = {s["verb"]: s for s in rec["steps"]}
    for verb in ("restore-regions", "erase-region", "erase-chip", "family-switch"):
        assert steps[verb]["result"] == exercise.SKIP and "this run" in steps[verb]["msg"], steps[verb]


def test_a_backup_play_that_names_no_chip_backup_fails_the_verb_and_the_erase_does_not_run(tmp_path):
    (tmp_path / "lay").mkdir()

    class NoChip(Plays):
        def __call__(self, spec):
            r = super().__call__(spec)
            r["out"] = r["out"].replace(f"chip {CHIP_STAMP}", "chip -")
            return r
    plays = NoChip()
    ex = make(tmp_path, plays, destructive=True)
    ex.v_update_there_and_back = lambda: (exercise.PASS, {}, "")
    rec = ex.go()
    steps = {s["verb"]: s for s in rec["steps"]}
    assert steps["backup-chip"]["result"] == exercise.FAIL and "named no chip backup" in steps["backup-chip"]["msg"]
    assert "erase-chip" not in plays.names()


def test_an_update_there_that_fails_still_comes_back(tmp_path, monkeypatch):
    (tmp_path / "lay").mkdir()
    states_in_place(monkeypatch)
    plays = Plays(fail={"update-there"})
    ex = make(tmp_path, plays)
    ex._backup()                                              # pylint: disable=protected-access
    result, ev, msg = ex.v_update_there_and_back()
    names = plays.names()
    assert result == exercise.FAIL and names[-2:] == ["update-there", "update-back"]
    assert ev["back_by"] == "apply" and "came back" in msg


def test_an_update_back_that_fails_falls_back_to_this_runs_chip_backup(tmp_path, monkeypatch):
    (tmp_path / "lay").mkdir()
    states_in_place(monkeypatch)
    plays = Plays(fail={"update-there", "update-back"})
    ex = make(tmp_path, plays)
    ex._backup()                                              # pylint: disable=protected-access
    result, ev, msg = ex.v_update_there_and_back()
    assert result == exercise.FAIL and plays.names()[-1] == "restore-chip-after-update"
    assert plays.specs[-1]["extra"] == {"restore_from": CHIP_STAMP}
    assert ev["back_by"] == "restore-chip" and "brought the image back" in msg


def test_an_update_back_that_fails_with_no_backup_of_this_run_restores_nothing(tmp_path, monkeypatch):
    (tmp_path / "lay").mkdir()
    states_in_place(monkeypatch)
    plays = Plays(fail={"update-there", "update-back"})
    ex = make(tmp_path, plays)
    result, _ev, msg = ex.v_update_there_and_back()
    assert result == exercise.FAIL and "restore-chip" not in " ".join(plays.names())
    assert "no chip backup" in msg


def test_a_play_the_board_is_not_in_is_no_pass_and_takes_no_backup(tmp_path):
    """ansible-playbook exits 0 for a play that matched no host: the board's recap line is the proof."""
    (tmp_path / "lay").mkdir()

    class Absent(Plays):
        def __call__(self, spec):
            r = super().__call__(spec)
            if spec["run"].rsplit("/", 1)[1][3:] in ("facts", "apply", "backup"):
                r["recap"] = {}
            return r
    plays = Absent()
    ex = make(tmp_path, plays, destructive=True)
    ex.v_update_there_and_back = lambda: (exercise.PASS, {}, "")
    rec = ex.go()
    steps = {s["verb"]: s for s in rec["steps"]}
    for verb in ("facts", "backup-chip"):
        assert steps[verb]["result"] == exercise.FAIL and "not in the play's recap" in steps[verb]["msg"], steps[verb]
    assert "erase-chip" not in plays.names() and not rec["passed"]
    result, _ev, msg = make(tmp_path, Absent()).v_apply()
    assert result == exercise.FAIL and "not in the play's recap" in msg


def test_a_board_unclean_at_the_start_and_untouched_is_not_tested_not_lost(tmp_path):
    (tmp_path / "lay").mkdir()
    rec = make(tmp_path, Plays(dirty_start=True, dirty_end=True)).go()
    step = next(s for s in rec["steps"] if s["verb"] == "came-back")
    assert rec["came_back"] is None and step["result"] == exercise.SKIP and step["msg"].startswith("not tested")
    rec = make(tmp_path / "b", Plays(dirty_start=True)).go()
    assert rec["came_back"] is False, "an unclean start that ends otherwise is a change, said so"


def test_without_confirm_apply_the_exercise_says_what_would_run_and_runs_nothing(monkeypatch, capsys):
    ran = []

    class Stub:
        def __init__(self, *a, **kw):
            self.verbs = exercise.applicable(FULL, IMAGES)
            self.fresh = {}

        def choose(self):
            pass

        def go(self):
            ran.append(True)
            return {"steps": [{"verb": "facts", "kind": "read", "result": "pass", "reason": "r", "msg": ""}],
                    "passed": True, "came_back": True, "seconds": 0, "path": "x"}
    monkeypatch.setattr(exercise, "Exercise", Stub)
    monkeypatch.setattr(exercise.site, "load_env", lambda: None)
    assert exercise.main(["lay", "b1", "--destructive"]) == 0 and ran == []
    assert "nothing was run; add confirm=apply" in capsys.readouterr().out
    assert exercise.main(["lay", "b1", "confirm=apply"]) == 0 and ran == [True]
    with pytest.raises(SystemExit):
        exercise.main(["lay", "b1", "confirm=yes"])
