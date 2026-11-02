"""The tool's tree stays a tool's: no dated notes, no first person, no private references.

A comment or doc explains behaviour, undated and in the third person; history belongs in
CHANGELOG.md, and hardware behaviour behind a rule in docs/HARDWARE.md.
"""
import os
import re
import subprocess

from tests.support.requirement import breach

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATE = re.compile(r"\b20\d\d-\d\d-\d\d\b")
# The same note dated short (`(11-04)`, `11-03:`, `on 10-26`), or pointing at a tracker no reader
# of the tree can open (`finding 72`, `bead .8`, any mention of a bead).
SHORT_DATE = re.compile(r"(?<![\w.x:/-])[01]\d-[0-3]\d(?=[:),;]| \d\d:\d\d)"
                        r"|\b(?i:on|since|from|until|before|after) [01]\d-[0-3]\d\b")
TRACKER = re.compile(r"\b(?:finding|FINDINGS?)s? \d+|\bbeads?\b(?! of\b)")
# A quoted date is a data value (a fixture's record), not a note.
QUOTED = re.compile(r"""["']20\d\d-\d\d-\d\d""")
# A dated observation is a fact, not history: what a device did, and when it was seen to (the
# hardware notes, or a line that says `observed <date>`). A tracker reference stays refused.
OBSERVED = re.compile(r"\bobserved\b", re.I)
OBSERVATIONS = ("docs/HARDWARE.md",)
EXEMPT = ("CHANGELOG.md", "tests/fixtures/", "tests/golden/", "collections/ansible_collections/rxrbench/core/host/venvs/")
#: Files that hold breaches on purpose: this check's own pattern examples, and the planted
#: breach every tree check is run against (test_checks_planted.py, docs/ARCHITECTURE.md#the-tools-own-tests).
PLANTED = ("tests/unit/test_hygiene.py", "tests/unit/test_checks_planted.py")
SUFFIXES = (".py", ".yml", ".yaml", ".j2", ".md", ".sh", ".txt", ".in", ".cfg", ".toml")


def tracked():
    """The tree's files as they stand: tracked, and new ones not yet added (not the ignored, not
    the deleted)."""
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True)
    return [n for n in out.stdout.split("\n") if n and os.path.lexists(os.path.join(REPO, n))]


SITE_LITERAL = re.compile(r"""['"]/site/|default=['"]site['"]|["']site["']\s*\)""")
SITE_SOURCES = ("collections/ansible_collections/rxrbench/core/plugins/module_utils/site.py",
                "collections/ansible_collections/rxrbench/core/playbooks/vars/site.yml")


def test_site_paths_come_from_one_place():
    """module_utils/site.py (and its Jinja mirror for the plain hosts inventory) are the only
    files that know where a site is; everything else reads bench_site_dir / bench_registry /
    bench_facts_dir, or asks site.py."""
    hits = []
    for name in tracked():
        if name in SITE_SOURCES or not name.endswith((".py", ".yml", ".yaml", ".j2")) \
                or name.startswith(("tests/", "site.example/", "docs/")):
            continue
        with open(os.path.join(REPO, name), errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if SITE_LITERAL.search(line):
                    hits.append(f"{name}:{i}: {line.strip()[:80]}")
    assert not hits, breach(
        ["a site path outside site.py:"] + hits,
        "where a site is, is known in one place: module_utils/site.py and its Jinja mirror "
        "(CONTRIBUTING.md#public-and-private); everything else reads bench_site_dir, bench_registry, "
        "bench_facts_dir or asks site.py",
        "ask site.py (site.site_dir(), site.registry_path(), ...) or read the bench_ variable",
        "a string that only looks like a site path (an example, a test's data): word it so it "
        "does not, or move it under tests/ or docs/, which this check leaves alone")


def test_the_note_patterns_tell_a_note_from_data():
    notes = ["# 11-03: a board", "(11-04)", "built on 10-26", "On 10-26 the board", "(the Pi, 11-05, at 12:00)",
             "finding 72", "FINDINGS 60, 61", "bead .8: a cert", "(bead `.73.13.5`)",
             "(bead: app-level config)", "parked as a low-priority bead"]
    data = ["0x10-0x20", "2.7-26", "1-1.4.1", "ports 10-20", "aa:bb:11-22", "10-12 boards",
            "finding the port", "a bead of solder"]
    hit = lambda s: bool(SHORT_DATE.search(s) or TRACKER.search(s))   # noqa: E731
    assert [s for s in notes if not hit(s)] == []
    assert [s for s in data if hit(s)] == []
    assert OBSERVED.search("observed 2026-11-04: the board reboots on a region write")


def test_no_dated_notes_in_the_tree():
    hits = []
    for name in tracked():
        if (name.startswith(EXEMPT) or os.path.basename(name) == "CHANGELOG.md"   # a collection's too
                or name in PLANTED   # breaches planted on purpose, as examples
                or not (name.endswith(SUFFIXES) or name == "justfile")):
            continue
        if os.path.islink(os.path.join(REPO, name)):
            continue
        with open(os.path.join(REPO, name), errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                observed = name in OBSERVATIONS or OBSERVED.search(line)
                dated = (DATE.search(line) and not QUOTED.search(line)) or SHORT_DATE.search(line)
                if (dated and not observed) or TRACKER.search(line):
                    hits.append(f"{name}:{i}: {line.strip()[:80]}")
    assert not hits, breach(
        ["dated notes or tracker references:"] + hits,
        "the tree describes behaviour as it is; history belongs in CHANGELOG.md, and a reader "
        "of the public tree can open every reference (docs/ARCHITECTURE.md#the-tools-own-tests)",
        "say the rule or the behaviour undated; move history to CHANGELOG.md; replace a tracker "
        "reference with what it says",
        "a date that is the fact recorded (when a device was seen to do something) goes in "
        "docs/HARDWARE.md or on a line that says `observed <date>`; keep the date")


ENV_READ = re.compile(r"""(?:environ(?:\.get)?\(|environ\[|getenv\(|lookup\('ansible\.builtin\.env',)\s*["']([A-Z][A-Z0-9_]+)["']""")
# Set by the tool for its own subprocesses (RXRBENCH_COLLECTIONS: tools/core_only.py's tree; UF2_ENTERS and UF2_RUNS: the
# UF2 flasher's hand-over to uf2_flash.py, docs/ARCHITECTURE.md#entering-uf2-mode) or its own units (the broker's environment file,
# written by the serial_broker role), or the system's: not settings.
ENV_INTERNAL = re.compile(r"^(RXRBENCH_COLLECTIONS|RXRBENCH_RELEASED|RXRBENCH_MODEM_REST|UF2_FLASH_REEXEC|UF2_LOCK_HELD|UF2_ENTERS$|UF2_RUNS$|CHIP_|OCD_|XDG_|PATH$|HOME$|USER$)")


def test_every_setting_is_in_env_example():
    """A setting the code reads is documented in .env.example, with its default and what reads it."""
    with open(os.path.join(REPO, ".env.example")) as fh:
        documented = set(re.findall(r"^#?([A-Z][A-Z0-9_]+)=", fh.read(), re.M))
    read = {}
    for name in tracked():
        if not name.endswith((".py", ".yml", ".yaml", ".j2")) or name.startswith("tests/"):
            continue
        with open(os.path.join(REPO, name), errors="replace") as fh:
            for var in ENV_READ.findall(fh.read()):
                if not ENV_INTERNAL.match(var):
                    read.setdefault(var, name)
    missing = {v: f for v, f in read.items() if v not in documented}
    assert not missing, breach(
        ["settings read but not in .env.example:"] + [f"{v} ({f})" for v, f in sorted(missing.items())],
        "every setting the code reads is documented in .env.example, with its default and what "
        "reads it (CONTRIBUTING.md#public-and-private)",
        "add it to .env.example, commented, with its default and the file that reads it",
        "a variable the tool sets for its own subprocesses, or the system's, is not a setting: "
        "add it to ENV_INTERNAL here, with the reason in its comment")


RECIPE = re.compile(r"^([a-z][a-z0-9-]*)(?:\s+[^:]*)?:\s*$", re.M)


def test_every_just_recipe_is_in_the_commands_doc():
    """docs/COMMANDS.md's command table names every recipe the justfile has (`default` lists them)."""
    with open(os.path.join(REPO, "justfile")) as fh:
        recipes = set(RECIPE.findall(fh.read())) - {"default"}
    with open(os.path.join(REPO, "docs", "COMMANDS.md")) as fh:
        readme = fh.read()
    missing = sorted(r for r in recipes if not re.search(r"`just " + re.escape(r) + r"\b", readme))
    assert not missing, "recipes not in docs/COMMANDS.md's table: " + ", ".join(missing)


def test_the_licence_text_is_what_the_metadata_declares():
    with open(os.path.join(REPO, "LICENSE")) as fh:
        assert "MIT License" in fh.read(200)
    for name in ("collections/ansible_collections/rxrbench/core/galaxy.yml",
                 "collections/ansible_collections/rxrbench/core/roles/board/meta/main.yml"):
        with open(os.path.join(REPO, name)) as fh:
            assert "MIT" in fh.read(), name


PLAN_FIRST = ("boot", "restore-regions", "restore-chip", "switch", "keys-restore", "erase", "erase-chip")


def test_a_recipe_that_writes_outside_apply_is_a_plan_unless_confirmed():
    """`just erase lab b1` must say what it would do; only `confirm=apply` lets it write."""
    with open(os.path.join(REPO, "justfile")) as fh:
        src = fh.read()
    for name in PLAN_FIRST:
        body = re.search(r"(?m)^" + re.escape(name) + r" [^\n]*:\n((?:    [^\n]*\n)+)", src).group(1)
        assert "confirm=apply" in body and '"--check --diff " + args' in body, name
        assert "{{args}}" not in body, f"{name}: its arguments reach Ansible only through the plan-or-apply choice"


def test_the_mcumgr_module_imports_without_pyyaml():
    """smp_flash runs in the bench host's MCUmgr venv (board_smp_python), which has smpclient
    and no PyYAML. Everything it imports must load without it; a plugin that reads YAML
    imports it where it reads."""
    import sys
    code = ("import sys\n"
            "class NoYaml:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'yaml' or name.startswith('yaml.'):\n"
            "            raise ImportError('no PyYAML in this venv')\n"
            "sys.meta_path.insert(0, NoYaml())\n"
            "import ansible_collections.rxrbench.core.plugins.modules.smp_flash\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": os.path.join(REPO, "collections")})
    assert r.returncode == 0, r.stderr[-600:]


# A comment says what must hold or why, in the third person, and names nothing a reader of the
# public tree cannot open. Not: first person ("we", "our firmware"), "the operator", stories of
# what once happened, moral tails ("worse than", "the hard way"), warning signs, or a private
# tool or document. `ours` counts only as prose ("is ours"), not as an identifier.
VOICE = re.compile(
    r"(?i)\b(we|our|ourselves)\b"
    r"|\b(as|not|of|is|are|be|was) ours\b"
    r"|\b(let|lets|for|to|tells|gives|under|behind|ahead of) us\b"
    r"|\bthe operator\b|\u26a0"
    r"|\bwas once\b|\bonce (found|left|made|flashed|turned|happened|grew|crashed|reported|broke|sat|failed)\b"
    r"|\bthat happened\b|\bworse than\b|\bthe hard way\b|\bexists to (prevent|refuse|explain)\b")
PRIVATE = re.compile(r"fleet\.py|DECLARATIVE-FLEET|main/src/|the bench's (proven|gated|fleet|smpmgr|recurring)")
VOICE_EXEMPT = ("tests/fixtures/", "tests/golden/") + PLANTED


def voice_files():
    for name in tracked():
        if name.startswith(VOICE_EXEMPT) or os.path.basename(name) == "CHANGELOG.md":
            continue
        if name.endswith(SUFFIXES) or name.endswith(".just") or name in ("justfile",) \
                or name.startswith("hooks/"):
            yield name


def test_comments_are_in_the_third_person_and_name_nothing_private():
    hits = []
    for name in voice_files():
        if os.path.islink(os.path.join(REPO, name)):
            continue
        with open(os.path.join(REPO, name), errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if VOICE.search(line) or PRIVATE.search(line):
                    hits.append(f"{name}:{n}: {line.strip()[:100]}")
    assert not hits, breach(
        ["first person, narrative or private references:"] + hits,
        "the public tree reads in the third person, says what holds rather than what happened, "
        "and names nothing a reader cannot open (CONTRIBUTING.md#public-and-private)",
        "say what must hold and why, in the third person; history to CHANGELOG.md; drop the "
        "private reference or say what it holds")


def test_the_voice_check_covers_the_tests_and_the_recipes():
    names = set(voice_files())
    assert any(n.startswith("tests/unit/") for n in names) and "justfile" in names
    assert any(n.endswith(".just") for n in names) and any(n.startswith("hooks/") for n in names)
    for prose in ("we write it", "our firmware", "the operator adds", "a board once flashed the wrong one",
                  "a stale alias is worse than none", "\u26a0 careful", "the board resets under us",
                  "the port is not ours"):
        assert VOICE.search(prose), prose
    for code in ("ours = uf2_mount(dev)", "if ours:", "out = {ours: j.get(theirs)}", "if self.ours else", "10 us timeout",
                 "run once per machine", "read once a run"):
        assert not VOICE.search(code), code
