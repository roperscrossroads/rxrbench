"""The three backup terms (docs/ARCHITECTURE.md#terms-chip-image-region-config): chip image, regions, config. The tool's old
names for the raw region level are gone from the tree; only the note that records the rename
and the changelogs, which record history, may name them."""
import os
import re
import subprocess

from tests.support.requirement import breach

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The old names: identifiers, the capability and role values, and the phrases that called raw
#: flash bytes "settings". Zephyr's settings store and Launcher's settings are upstream names
#: (docs/ARCHITECTURE.md#terms-chip-image-region-config) and never match these.
OLD = re.compile(
    r"\bsettings_(?:backup|restore|erase|regions|flashers)\b|\bboard_erase\b"
    r"|['\"]settings['\"] *(?:not )?in bench_can|\(\"settings\", \"chip\"\)|role\??: *settings\b|\ba settings role\b"
    r"|\bsettings (?:level|backups?|restores?|erase|regions?|partitions?)\b", re.I)

#: Where the old names may stay: the record of the rename, and history.
ALLOWED = ("docs/ARCHITECTURE.md#terms-chip-image-region-config", "CHANGELOG.md", "tests/unit/test_terms.py",
           "tests/unit/test_checks_planted.py")


def tracked():
    out = subprocess.run(["git", "-C", REPO, "ls-files"], capture_output=True, text=True,
                         check=True).stdout.split()
    return [f for f in out if os.path.isfile(os.path.join(REPO, f))]


def test_the_old_names_for_the_region_level_are_gone():
    hits = []
    for rel in tracked():
        if rel.endswith(ALLOWED):
            continue
        try:
            with open(os.path.join(REPO, rel), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{rel}:{n}: {line.strip()[:90]}" for n, line in enumerate(lines, 1)
                 if OLD.search(line)]
    assert not hits, breach(
        hits, "docs/ARCHITECTURE.md#terms-chip-image-region-config: chip image, regions and config each name one thing; the "
              "raw ranges of flash are regions (region_backup, region_restore, region_erase, "
              "the capability and role names), never \"settings\"",
        "use the term docs/ARCHITECTURE.md gives: region(s) for raw named ranges, chip image for the whole "
        "flash, config for what the firmware itself reads",
        "an upstream's own name (Zephyr's settings, Launcher's settings) that this pattern "
        "catches: narrow the pattern here, with the case as a test")
