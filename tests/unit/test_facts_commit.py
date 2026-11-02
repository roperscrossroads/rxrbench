"""One `chore(facts)` commit per run, only when the site opted in, only the facts, never a tree
with other changes in it."""
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(REPO, "tools", "facts_commit.py")


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def site(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.org")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "registry.yaml").write_text("version: 1\ndevices: {}\n")
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "a.yaml").write_text("key: a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path, facts


def run(facts, message="survey on bench1, 2 devices", **env):
    return subprocess.run([TOOL, "--facts-dir", str(facts), "--message", message], capture_output=True,
                          text=True, env={**os.environ, "RXRBENCH_FACTS_COMMIT": "", **env})


def test_nothing_happens_without_the_opt_in(site):
    top, facts = site
    (facts / "a.yaml").write_text("key: b\n")
    r = run(facts)
    assert r.returncode == 0 and "not committed" in r.stdout
    assert git(top, "status", "--porcelain"), "the tree is left as it was"


def test_changed_facts_are_committed_as_one_batch(site):
    top, facts = site
    (facts / "a.yaml").write_text("key: b\n")
    (facts / "new.yaml").write_text("key: n\n")
    r = run(facts, RXRBENCH_FACTS_COMMIT="1")
    assert r.returncode == 0 and "committed 2 file(s)" in r.stdout, r.stdout + r.stderr
    assert git(top, "log", "-1", "--format=%s") == "chore(facts): survey on bench1, 2 devices"
    assert git(top, "status", "--porcelain") == ""
    again = run(facts, RXRBENCH_FACTS_COMMIT="1")
    assert "unchanged" in again.stdout


def test_a_tree_with_other_changes_is_left_alone_and_named(site):
    top, facts = site
    (facts / "a.yaml").write_text("key: b\n")
    (top / "registry.yaml").write_text("version: 1\ndevices: {x: {}}\n")
    r = run(facts, RXRBENCH_FACTS_COMMIT="1")
    assert r.returncode == 0 and "left uncommitted" in r.stdout and "registry.yaml" in r.stdout
    assert git(top, "log", "-1", "--format=%s") == "init"


def test_an_untracked_file_outside_the_facts_does_not_block_the_batch(site):
    """A lab tree carries notes in progress and scratch logs; a commit of the facts path alone
    never picks them up, so they are no reason to hold the facts back."""
    top, facts = site
    (facts / "a.yaml").write_text("key: b\n")
    (top / "SCRATCH.md").write_text("in progress\n")
    r = run(facts, "survey on bench1, 1 device", RXRBENCH_FACTS_COMMIT="1")
    assert r.returncode == 0 and "committed 1 file(s)" in r.stdout, r.stdout + r.stderr
    status = subprocess.run(["git", "-C", str(top), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert status.strip() == "?? SCRATCH.md"


def test_a_facts_directory_outside_git_is_reported_not_an_error(tmp_path):
    facts = tmp_path / "facts"
    facts.mkdir()
    r = subprocess.run([TOOL, "--facts-dir", str(facts), "--message", "x"], capture_output=True, text=True,
                       env={**os.environ, "RXRBENCH_FACTS_COMMIT": "1", "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)})
    assert r.returncode == 0 and "not in a git work tree" in r.stdout
