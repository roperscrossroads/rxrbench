#!/usr/bin/env python3
"""show.py <layout> <board> [key.path] [--ansible] [--json]: a board's declared state with provenance.

Every leaf is printed with the file that set it: a layer's group_vars, a profile, or the
layout's own group_vars / host_vars (module_utils/layers.py composes them the way the inventory
plugin and Ansible do). A dotted key narrows it. `--ansible` also asks ansible-inventory for the
host and reports every declared key whose value differs: the two must agree, always.

show.py <layout> --layers: the layer stack of a layout, lowest first, and what each brings.
"""
import json
import os
import subprocess
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import layers, site  # noqa: E402

site.load_env()


def _config(layout_dir):
    with open(os.path.join(layout_dir, "devreg.yml")) as fh:
        return yaml.safe_load(fh) or {}


def _walk(value, path=()):
    if isinstance(value, dict) and value:
        for k, v in value.items():
            yield from _walk(v, path + (k,))
    else:
        yield path, value


def _dig(value, path):
    for k in path:
        if not isinstance(value, dict) or k not in value:
            sys.exit(f"no key {'.'.join(path)}")
        value = value[k]
    return value


def render(vars_, prov, key=()):
    """YAML of the value, each leaf followed by `# <- source`."""
    sub = _dig(vars_, key)
    text = yaml.safe_dump({key[-1]: sub} if key else sub, sort_keys=True, default_flow_style=False,
                          allow_unicode=True, width=10 ** 6)
    # Annotate: a leaf line is one whose key path is in prov. Match by walking the dumped
    # structure in the same order yaml.safe_dump used (sorted keys).
    labels = {}
    base = key[:-1] if key else ()
    for path, _ in _walk({key[-1]: sub} if key else sub):
        labels[path] = prov.get(base + path, "?")
    out, stack = [], []
    for line in text.rstrip("\n").split("\n"):
        indent = (len(line) - len(line.lstrip(" "))) // 2
        stripped = line.strip()
        if stripped.startswith("- "):
            out.append(line)
            continue
        k = stripped.split(":", 1)[0].strip("'\"")
        stack = stack[:indent] + [k]
        path = tuple(stack)
        if path in labels and not stripped.endswith(":") and not stripped.endswith(": |-") \
                and not stripped.endswith(": >-"):
            out.append(f"{line}    # <- {labels[path]}")
        elif path in labels:
            out.append(f"{line}    # <- {labels[path]}")
        else:
            out.append(line)
    return "\n".join(out)


def ansible_host(layout_dir, board):
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-inventory"), "-i", layout_dir,
                        "--host", board], capture_output=True, text=True, cwd=REPO)
    if r.returncode != 0:
        sys.exit(r.stderr.strip() or f"ansible-inventory failed ({r.returncode})")
    return json.loads(r.stdout)


def main(argv):
    if len(argv) >= 2 and argv[1] == "--layers":
        d = site.layout_path(argv[0])
        print(layers.describe(d, _config(d)))
        return 0
    if len(argv) < 2:
        sys.exit(__doc__)
    layout, board = argv[0], argv[1]
    check = "--ansible" in argv[2:]
    as_json = "--json" in argv[2:]
    rest = [a for a in argv[2:] if a not in ("--ansible", "--json")]
    key = tuple(rest[0].split(".")) if rest else ()
    d = site.layout_path(layout)
    cfg = layers.expand(d, _config(d))   # a variant's borrowed boards count
    if board not in (cfg.get("boards") or {}):
        sys.exit(f"{board} is not a board of layout {layout}")
    vars_, prov = layers.effective(d, cfg, board)
    if as_json:
        # For other tools (a test suite reads the declared radio state from here): the
        # effective vars, narrowed by the key, with no provenance.
        sub = vars_
        for k in key:
            sub = (sub or {}).get(k)
        print(json.dumps(sub, default=str))
        return 0
    print(render(vars_, prov, key))
    if check:
        got = ansible_host(d, board)
        bad = [k for k, v in vars_.items() if got.get(k) != v]
        if bad:
            print(f"\nDIFFERS from ansible-inventory: {', '.join(sorted(bad))}", file=sys.stderr)
            return 1
        print(f"\nansible-inventory agrees on all {len(vars_)} declared keys", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
