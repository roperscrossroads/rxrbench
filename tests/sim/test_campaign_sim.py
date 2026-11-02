"""A campaign on the simulated Zephyr bench (docs/ARCHITECTURE.md#runs-tests-and-campaigns): the example campaign
`zsim-images` moves zb1 to its image b and back, each state applied, checked clean and tested;
a campaign whose apply fails stops there and later states never run."""
import json
import os
import subprocess

from tests.sim.test_zephyr_sim import REPO, VENV, bench, pytestmark  # noqa: F401  pylint: disable=unused-import


def campaign(run, name, site="site.example"):
    env = {**os.environ, "RXRBENCH_SIM_RUN": run, "ANSIBLE_FORCE_COLOR": "0", "RXRBENCH_SITE": site,
           "PATH": VENV + os.pathsep + os.environ["PATH"]}
    r = subprocess.run([os.path.join(VENV, "python"), "tools/campaign.py", name], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=1500, check=False)
    out = r.stdout + r.stderr
    path = next(line.rsplit(": ", 1)[1] for line in out.splitlines()
                if line.startswith(f"campaign {name}: ") and line.endswith("campaign.json"))
    with open(path) as f:
        return r.returncode, out, json.load(f)


def test_the_example_campaign_moves_zb1_to_image_b_and_back_with_its_tests_in_each(bench):
    rc, out, rec = campaign(bench, "zsim-images")
    assert rc == 0 and rec["passed"], out
    assert [s["label"] for s in rec["states"]] == ["a", "b", "back"]
    assert all(s["clean"] and not s["stopped"] for s in rec["states"]), rec
    b = rec["states"][1]
    assert b["overrides"] == {"boards": {"zb1": {"firmware": {"family": "bare", "image": "zephyr-shell-b"}}}}
    with open(os.path.join(b["dir"], "state.json")) as f:
        state = json.load(f)
    assert state["clean"] and state["boards"]["zb1"]["declared"]["image"] == "zephyr-shell-b"
    with open(os.path.join(b["dir"], "scenario", "zephyr-shells-answer", "scenario.json")) as f:
        assert json.load(f)["passed"]
    assert "device tests" not in b["steps"] and "device tests" in rec["states"][0]["steps"]
    with open(os.path.join(rec["states"][2]["dir"], "state.json")) as f:
        assert json.load(f)["boards"]["zb1"]["declared"]["image"] == "zephyr-shell", "and back"
    # and compare reads the campaign: zb1's image changed between states, its test did not
    r = subprocess.run([os.path.join(VENV, "python"), "tools/compare.py", "--json",
                        os.path.relpath(os.path.dirname(rec["states"][0]["dir"]), os.path.join(REPO, "runs"))],
                       cwd=REPO, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    first, second = json.loads(r.stdout)
    assert [s["board"] for s in first["state"]] == ["zb1"] and [s["board"] for s in second["state"]] == ["zb1"]
    assert {(x["name"], x["verdict"]) for x in first["rows"] if x["what"] == "scenario"} >= {("zephyr-shells-answer", "same")}


def test_a_failed_apply_stops_the_campaign(bench, tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    for entry in os.listdir(os.path.join(REPO, "site.example")):
        os.symlink(os.path.join(REPO, "site.example", entry), site / entry)
    os.unlink(site / "campaigns")
    (site / "campaigns").mkdir()
    (site / "campaigns" / "wrong.yml").write_text(
        "states:\n"
        "  - {state: zsim, label: a, tests: [], device_tests: false}\n"
        # another family's image on zb1, with no first-flash go-ahead: the apply refuses it
        "  - {state: zsim, label: wrong, boards: {zb1: {firmware: {family: zephyr_mcumgr, image: zsmp-b}}}}\n"
        "  - {state: zsim, label: never}\n")
    rc, out, rec = campaign(bench, "wrong", str(site))
    assert rc != 0 and not rec["passed"], out
    assert [s["label"] for s in rec["states"]] == ["a", "wrong"], "the third state never ran"
    assert rec["states"][1]["stopped"] == "the apply failed", rec
    assert "plan after" not in rec["states"][1]["steps"]
