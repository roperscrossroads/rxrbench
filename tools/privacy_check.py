#!/usr/bin/env python3
"""Refuse site-private text in the tool's repository.

    privacy_check.py            every tracked file
    privacy_check.py --staged   what is about to be committed (the pre-commit hook)
    privacy_check.py --msg F    a commit message (the commit-msg hook)

rxrbench is meant to be published; a site's hostnames, users, addresses, paths and trackers
are not. Two pattern lists, one regular expression per line, `#` comments allowed:

- the generic one, here in `privacy-patterns.txt` (public): private-range IPv4 addresses,
  `.home.arpa` / `.lan` names, `/home/<user>/` paths, email addresses;
- the site's own, `<site>/private-patterns.txt` (private, never in this repo): that site's
  hostnames, users, domains, paths and tracker ids. The list of what is private is itself
  private, so it lives with the site.

Every hardware identifier the site knows is private too, with no list to keep: the chip ids,
MACs and node ids in its registry and survey facts are added automatically. A workstation may
hold other sites' boards too (a second bench's), which no site file here names: so any token
shaped like a chip id (16 hex digits) or a MAC is refused unless it is plainly made up (a
regular pattern: 0A0B0C0D0E0F1011, 0123456789ABCDEF, a locally administered MAC padded with
zeros) or listed, reviewed, in `tests/fixtures/made-up-ids.txt`.

A line may opt out of one check with `privacy: allow` in it (an example address in a doc that
must stay, say); use it sparingly, and never for a real site value. Binary files are skipped.
Exit 1, with every hit listed, when anything matches.
"""
import argparse
import os
import re
import subprocess
import sys

import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402

GENERIC = os.path.join(site.REPO, "privacy-patterns.txt")
ALLOW = "privacy: allow"
MADE_UP = os.path.join(site.REPO, "tests", "fixtures", "made-up-ids.txt")
ID_SHAPES = re.compile(r"(?<![0-9A-Za-z_-])(?:[0-9A-Fa-f]{16}|[0-9A-F]{12}|(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})"
                       r"(?![0-9A-Za-z_])|(?<=_)[0-9A-F]{12,16}(?=-if)")


def made_up(token):
    """Is an id-shaped token plainly invented: a regular pattern no chip or MAC would have?"""
    t = re.sub(r"[:-]", "", token).upper()
    if len(set(t)) <= 4:
        return True
    nibbles = [int(c, 16) for c in t]
    if len({(b - a) % 16 for a, b in zip(nibbles, nibbles[1:])}) == 1:
        return True                                     # 0123456789ABCDEF, FEDC…
    pairs = [int(t[i:i + 2], 16) for i in range(0, len(t), 2)]
    if len({(b - a) % 256 for a, b in zip(pairs, pairs[1:])}) == 1:
        return True                                     # 0A0B0C0D…, 0A1B2C3D4E5F
    if len(t) == 12 and pairs[0] & 0x02 and pairs[1:5].count(0) >= 3:
        return True                                     # 02:00:00:00:00:xx, locally administered
    return False


def made_up_list():
    if not os.path.exists(MADE_UP):
        return set()
    with open(MADE_UP) as fh:
        return {re.sub(r"[:-]", "", ln.split("#")[0].strip()).upper() for ln in fh
                if ln.split("#")[0].strip()}


def id_hits(name, data, listed):
    """Tokens shaped like a chip id or a MAC that are neither plainly made up nor listed."""
    if b"\0" in data[:8192] or name == os.path.relpath(MADE_UP, site.REPO):
        return []
    hits = []
    for i, line in enumerate(data.decode(errors="replace").splitlines(), 1):
        if ALLOW in line:
            continue
        for m in ID_SHAPES.finditer(line):
            tok = m.group(0)
            if not made_up(tok) and re.sub(r"[:-]", "", tok).upper() not in listed:
                hits.append(f"{name}:{i}: {tok!r} (an id-shaped token that is neither plainly "
                            f"made up nor in tests/fixtures/made-up-ids.txt)")
    return hits


def patterns(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append((re.compile(line, re.I), line))
    return out


MAC = re.compile(r"\b([0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", re.I)
CHIP = re.compile(r"\b[0-9a-f]{16}\b", re.I)
NODE = re.compile(r"\b(?:0x)?([0-9a-f]{8})\b", re.I)


def _leaves(value, key):
    """Every scalar under a key named `key`, anywhere in a YAML document."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key and isinstance(v, (str, int)):
                yield str(v)
            else:
                yield from _leaves(v, key)
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v, key)


def hardware_patterns():
    """The site's own chip ids, MACs and node ids, from its registry and facts, as literal
    patterns. A MAC also in its bare-hex spelling (as it appears in facts keys); a node id (the
    value of any `node_id` key: `0x` and 8 hex digits) also without its `0x`, so a bare or
    upper-case spelling in a test is a hit too."""
    import yaml  # pylint: disable=import-outside-toplevel
    docs = []
    reg = site.registry_path()
    if os.path.exists(reg):
        with open(reg) as fh:
            docs.append((yaml.safe_load(fh) or {}).get("devices") or {})
    facts = site.facts_dir()
    if os.path.isdir(facts):
        for f in os.listdir(facts):
            if f.endswith(".yaml"):
                with open(os.path.join(facts, f)) as fh:
                    docs.append(yaml.safe_load(fh) or {})
    found = set()
    for doc in docs:
        text = yaml.safe_dump(doc)
        found.update(m.group(0).upper().replace("-", ":") for m in MAC.finditer(text))
        found.update(m.group(0).upper() for m in CHIP.finditer(text))
        for v in _leaves(doc, "node_id"):
            m = NODE.fullmatch(v.strip())
            if m:
                found.add("0x" + m.group(1).lower())
    out = []
    for v in sorted(found):
        bare = v.replace(":", "").removeprefix("0x")
        if len(set(bare)) <= 2:      # 0000…, FFFF…: placeholders, not hardware
            continue
        for spelling in {v, bare}:
            # Not \b: in a by-id name the id follows `_` (usb-Board_<serial>-if00), which \b
            # takes for part of the word. No hex digit on either side is what makes it the id.
            out.append((re.compile(r"(?<![0-9a-f])" + re.escape(spelling) + r"(?![0-9a-f])", re.I),
                        f"site hardware id {v}"))
    return out


def git(*args):
    return subprocess.run(["git", *args], cwd=site.REPO, capture_output=True, check=True).stdout


def files(staged):
    if staged:
        names = git("diff", "--cached", "--name-only", "--diff-filter=ACMR").decode().split("\n")
        return [(n, git("show", f":{n}")) for n in names if n]
    names = git("ls-files").decode().split("\n")
    out = []
    for n in names:
        p = os.path.join(site.REPO, n)
        if n and os.path.isfile(p) and not os.path.islink(p):
            with open(p, "rb") as fh:
                out.append((n, fh.read()))
    return out


def scan(name, data, pats):
    if b"\0" in data[:8192]:
        return []
    hits = []
    for i, line in enumerate(data.decode(errors="replace").splitlines(), 1):
        if ALLOW in line:
            continue
        for rx, src in pats:
            m = rx.search(line)
            if m:
                hits.append(f"{name}:{i}: {m.group(0)!r} (pattern {src})")
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--msg")
    a = ap.parse_args()
    site.load_env()
    pats = patterns(GENERIC) + patterns(os.path.join(site.site_dir(), "private-patterns.txt")) \
        + hardware_patterns()
    if a.msg:
        with open(a.msg, "rb") as fh:
            hits = scan("commit message", fh.read(), pats)
    else:
        listed = made_up_list()
        hits = [h for n, d in files(a.staged) for h in scan(n, d, pats) + id_hits(n, d, listed)]
    for h in hits:
        print(h)
    if hits:
        print(f"\n{len(hits)} site-private match(es): move the text to the site, or make it an "
              "example (see CONTRIBUTING.md#public-and-private)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
