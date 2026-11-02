#!/usr/bin/env python3
"""Stage a catalogued image from its source into the image store.

    tools/stage_catalog.py <image> [--board-tag <tag>] [--store <dir>]

The image is an entry of the firmware catalogs (core's, each plugin's, the site's
`catalog/firmware.yml`) whose `source` is a published file: `github` or `url`, pinned by its
sha256, with `member:` for one file of a zip. The source is fetched and checked
(module_utils/artifact.py), and the file staged as `<store>/<family>/<image>/` with its
manifest (tools/stage_raw.py), which is what a layout's `bench_image` names.

An `esp-image` entry whose `member` is a release's merged image is staged the way the release's
full install writes it: the other files of the same zip that go into the image's own partitions
(an OTA app, a file system) are laid into it (stage_raw.py --part). Which files go where is the
family's plugin's to say (`release_partitions`, families/base.py; the mesh plugin's stock
Meshtastic family reads the release's `.mt.json`, each file checked against the md5 it gives), or the entry's own `partition_images: {<partition label or
subtype>: <member>}`, which wins.

The entry may say what the firmware reports of itself, so a board running the image is told
from one that is not: `version`, and `build` (a release whose version ends in its commit
reports that commit as its build; given as a version alone, the commit is taken from it).
`board_tags` must name one type, or --board-tag picks one of them.

A `path` source is a directory something else stages: nothing to fetch. Staged already, the
image is reported and the exit is 0; not there, it says where it is expected and how to stage it.
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  (the collections first on sys.path)

from ansible_collections.rxrbench.core.plugins.module_utils import artifact, layout, site  # noqa: E402
from ansible_collections.rxrbench.core.plugins.plugin_utils import families, registry  # noqa: E402

FORMATS = (".uf2", ".bin", ".hex", ".ihex", ".elf")


def build_of(entry):
    """The build id an image reports: the entry's own, else a commit its version ends in."""
    if entry.get("build"):
        return str(entry["build"])
    m = re.search(r"\.([0-9a-f]{7,40})$", str(entry.get("version") or ""))
    return m.group(1) if m else None


def already_there(name, src):
    """A path source: nothing to fetch. Staged already is done (exit 0); not there yet says
    where it is expected and what stages it (exit 1: a later apply would not find it)."""
    where = os.path.expanduser(str(src.get("path") or ""))
    where = where if os.path.isabs(where) else os.path.join(REPO, where)
    if os.path.exists(where):
        print(f"{name}: its source is a path, staged already: {where}")
        sys.exit(0)
    sys.exit(f"{name}: its source is a path, expected at {where} and not there: stage it there "
             "with `just stage-image` (a firmware build) or tools/stage_raw.py (a single image file)")


def partition_images(name, entry):
    """[(partition, local file)] to lay into the merged image: the entry's own
    `partition_images`, else what the family's plugin says the release writes beside it."""
    src = entry.get("source") or {}
    wanted = entry.get("partition_images")
    if wanted and not src.get("member"):
        sys.exit(f"{name}: partition_images are members of the zip its `member` comes from")
    if wanted:
        rows = [{"partition": label, "member": member} for label, member in wanted.items()]
    elif entry.get("flasher") == "esp-image" and src.get("member"):
        rows = families.plugin(entry["family"]).release_partitions(
            src["member"], lambda member: read_member(name, src, member))
    else:
        return []
    out = []
    for row in rows:
        try:
            path = artifact.fetch({**src, "member": row["member"]})
        except artifact.ArtifactError as e:
            sys.exit(f"{name}: {e}")
        if row.get("md5"):
            with open(path, "rb") as f:
                got = hashlib.md5(f.read(), usedforsecurity=False).hexdigest()
            if got != row["md5"]:
                sys.exit(f"{name}: {row['member']} has md5 {got}, the release says {row['md5']}")
        out.append((row["partition"], path))
    return out


def read_member(name, src, member):
    """One file of the zip a source's `member` comes from, or None when it has no such file."""
    try:
        with zipfile.ZipFile(artifact.fetch({k: v for k, v in src.items() if k != "member"})) as z:
            return z.read(member) if member in z.namelist() else None
    except (artifact.ArtifactError, zipfile.BadZipFile) as e:
        sys.exit(f"{name}: {e}")


def plan(name, catalog, board_tag=None):
    """(entry, board tag) for an image, or exit with why it cannot be staged here."""
    entry = catalog.get(name)
    if entry is None:
        sys.exit(f"{name}: not in the firmware catalogs ({', '.join(registry.catalog_dirs())})")
    src = entry.get("source") or {}
    if src.get("type") == "path":
        already_there(name, src)
    problems = artifact.check_source(src)
    if problems:
        sys.exit(f"{name}: " + "; ".join(problems))
    tags = list(entry.get("board_tags") or [])
    if board_tag:
        if board_tag not in tags:
            sys.exit(f"{name}: is for {', '.join(tags) or 'no board type'}, not {board_tag}")
        tags = [board_tag]
    if len(tags) != 1:
        sys.exit(f"{name}: is for {', '.join(tags) or 'no board type'}: name one with --board-tag")
    return entry, tags[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image")
    ap.add_argument("--board-tag")
    ap.add_argument("--store", default=os.path.join(REPO, ".build", "store"))
    a = ap.parse_args()
    site.load_env()
    catalog = layout.load_catalog(*registry.catalog_dirs())["images"]
    entry, tag = plan(a.image, catalog, a.board_tag)
    try:
        path = artifact.fetch(entry["source"])
    except artifact.ArtifactError as e:
        sys.exit(f"{a.image}: {e}")
    if not path.lower().endswith(FORMATS):
        sys.exit(f"{a.image}: {os.path.basename(path)} is not a single image file this tool "
                 f"stages ({', '.join(FORMATS)})")
    argv = [sys.executable, os.path.join(REPO, "tools", "stage_raw.py"), path, "--name", a.image,
            "--board-tag", tag, "--family", entry["family"], "--store", a.store]
    for label, part in partition_images(a.image, entry):
        argv += ["--part", f"{label}={part}"]
    if entry.get("version"):
        argv += ["--version", str(entry["version"])]
    if build_of(entry):
        argv += ["--build", build_of(entry)]
    sys.exit(subprocess.run(argv, check=False).returncode)


if __name__ == "__main__":
    main()
