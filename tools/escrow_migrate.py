#!/usr/bin/env python3
"""Rewrite a site's escrow files into the one shape every secret kind has.

    tools/escrow_migrate.py [--identity-dir DIR] [--check]

An escrow file (`identity/<board>.sops.yaml`, written by `just keys-export`) once kept a
board's identity key as a record per family at the top (`<family>: {node_id, private_key,
public_key_fp, exported}`); every kind is now under `secrets.<family>.<kind>` with `{id, value,
fingerprint, exported}` (core/plugins/filter/escrow.py). This moves each such record there.

Each file is decrypted into memory, rewritten, encrypted from stdin under its own path's
creation rule (`sops --filename-override`), and read back: the record must come back equal,
or the file is left as it was. Nothing it prints is key material: the board, the family, the
key's id and fingerprint. A copy of the directory, still encrypted, is kept first beside it
(`<dir>.bak-<stamp>`). A top-level record that is not an identity record (no node_id,
private_key or public_key_fp) is not this tool's to move and is left as it is, said by name.
`--check` says what would move and writes nothing.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  (the collections first on sys.path)

from ansible_collections.rxrbench.core.plugins.filter import escrow  # noqa: E402
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402

OLD = {"node_id": "id", "private_key": "value", "public_key_fp": "fingerprint",
       "exported": "exported"}
KEEP = ("board", "secrets", "sops")


def sops_binary():
    """sops on PATH, else the pinned one (tools/pins.py: ~/.local/share/rxrbench/bin)."""
    found = shutil.which("sops") or shutil.which(
        "sops", path=os.path.expanduser("~/.local/share/rxrbench/bin"))
    if not found:
        sys.exit("escrow_migrate: no sops on PATH or in ~/.local/share/rxrbench/bin "
                 "(`just setup` installs the pinned one)")
    return found


def run(sops, args, data=None, cwd=None):
    """sops, run where the file is: it finds the site's .sops.yaml upward from its working
    directory."""
    r = subprocess.run([sops, *args], input=data, capture_output=True, check=False, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip().splitlines()[-1:] or "sops failed")
    return r.stdout


def migrated(doc):
    """(the document in the one shape, [family moved], [top-level key left alone])."""
    out = {k: v for k, v in doc.items() if k in KEEP}
    moved, left = [], []
    for key, value in doc.items():
        if key in KEEP:
            continue
        if isinstance(value, dict) and any(f in value for f in ("node_id", "private_key", "public_key_fp")):
            record = {new: value.get(old) for old, new in OLD.items()}
            out = escrow.escrow_put(out, doc.get("board"), key, "identity", record, record["exported"])
            moved.append(key)
        else:
            out[key] = value
            left.append(key)
    return out, moved, left


def migrate(sops, path, check):
    here = os.path.dirname(path)
    plain = yaml.safe_load(run(sops, ["--decrypt", path], cwd=here)) or {}
    doc = {k: v for k, v in plain.items() if k != "sops"}
    new, moved, left = migrated(doc)
    name = os.path.basename(path)
    for key in left:
        print(f"{name}: `{key}` is not an identity record: left as it is")
    if not moved:
        print(f"{name}: in the one shape already" if not left else f"{name}: nothing to move")
        return False
    for fam in moved:
        rec = escrow.escrow_get(new, fam, "identity")
        print(f"{name}: {fam} identity {rec.get('id')} (fingerprint {rec.get('fingerprint')})"
              f"{'' if check else ' -> secrets.' + fam + '.identity'}")
    if check:
        return True
    body = yaml.safe_dump(new, sort_keys=False).encode()
    enc = run(sops, ["--encrypt", "--filename-override", path, "--input-type", "yaml",
                     "--output-type", "yaml", "/dev/stdin"], body, cwd=here)
    tmp = path + ".migrating"
    with open(tmp, "wb") as f:
        f.write(enc)
    back = yaml.safe_load(run(sops, ["--decrypt", "--input-type", "yaml", "--output-type", "yaml",
                                     tmp], cwd=here))
    for fam in moved:
        was = {new_k: doc[fam].get(old) for old, new_k in OLD.items()}
        if escrow.escrow_get(back, fam, "identity") != was:
            os.unlink(tmp)
            raise RuntimeError(f"{name}: {fam}'s record did not read back equal: left as it was")
    os.replace(tmp, path)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--identity-dir", default=os.path.join(site.site_dir(), "identity"))
    ap.add_argument("--check", action="store_true", help="say what would move, write nothing")
    a = ap.parse_args()
    sops = sops_binary()
    files = sorted(os.path.join(a.identity_dir, f) for f in os.listdir(a.identity_dir)
                   if f.endswith(".sops.yaml"))
    if not a.check:
        backup = f"{a.identity_dir.rstrip('/')}.bak-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        shutil.copytree(a.identity_dir, backup)
        print(f"encrypted copy: {backup}")
    failed = 0
    for path in files:
        try:
            migrate(sops, path, a.check)
        except (RuntimeError, yaml.YAMLError) as e:
            failed += 1
            print(f"{os.path.basename(path)}: {e}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
