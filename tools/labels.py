#!/usr/bin/env python3
"""labels.py <layout> [--out FILE]: a printable sheet of board labels for a layout.

One card per board: its name (large), its board type, the tail of the serial or MAC it is
recognised by, its bench host, and its LED colour (what it is for, as the layout gives it:
roles/board `board_led_colors`). Read from the layout's inventory, so it says what `just
apply` would make the boards. Writes runs/labels-<layout>.html; print it at 100 %.
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402

site.load_env()
SWATCH = {"red": "#d33", "green": "#2a2", "blue": "#26d", "yellow": "#eb0", "cyan": "#1bc",
          "magenta": "#c3c", "default": "#999"}


def plain(v):
    """A value as ansible-inventory --list prints it, without its {"__ansible_unsafe": ...}
    wrapper (a string from a template comes out wrapped)."""
    if isinstance(v, dict):
        return v["__ansible_unsafe"] if set(v) == {"__ansible_unsafe"} else \
            {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


def inventory(layout_dir):
    out = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-inventory"), "-i",
                          layout_dir, "--list"], capture_output=True, text=True, cwd=REPO,
                         check=True).stdout
    return json.loads(out)


def recognised_by(dev):
    """The tail of what the registry matches the board by: its serial or MAC."""
    m = dev.get("match") or {}
    for k in ("usb_serial", "serial_by_id"):
        if m.get(k):
            # a by-id name ends in its serial, then -if00 (and -port0 for a bridge)
            serial = re.sub(r"-if\d+(-port\d+)?$", "", str(m[k])).rsplit("_", 1)[-1]
            tail = re.sub(r"[^0-9A-Fa-f]", "", serial)[-6:]
            return tail.upper() or None
    return None


def card(name, dev, hv, color, host):
    kind = (dev.get("description") or "").split(" — ")[0].split(" (")[0][:40]
    fw = hv.get("firmware") or {}
    what = fw.get("family", "")
    key = hv.get("bench_identity_key")
    if key and fw.get(key) is not None:
        what += f" {key} {fw[key]}"
    sw = (f'<span class="sw" style="background:{SWATCH.get(color, "#999")}"></span>{color}'
          if color else "")
    return (f'<div class="card"><div class="name">{html.escape(name)}</div>'
            f'<div class="kind">{html.escape(kind)}</div>'
            f'<div class="row"><span>{html.escape(what)}</span>'
            f'<span>{html.escape(recognised_by(dev) or "")}</span></div>'
            f'<div class="row"><span>{html.escape(host)}</span>'
            f'<span>{sw}</span></div></div>')


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Board labels: {layout}</title>
<style>
@page {{ size: A4; margin: 10mm; }}
body {{ font-family: sans-serif; margin: 0; background: #fff; color: #000; }}
.sheet {{ display: grid; grid-template-columns: repeat(3, 60mm); gap: 4mm; }}
.card {{ width: 60mm; height: 28mm; border: 0.3mm dashed #888; box-sizing: border-box;
        padding: 2mm 3mm; display: flex; flex-direction: column; justify-content: space-between; }}
.name {{ font-size: 18pt; font-weight: bold; }}
.kind {{ font-size: 7pt; color: #333; }}
.row {{ display: flex; justify-content: space-between; font-size: 8pt; }}
.sw {{ display: inline-block; width: 3mm; height: 3mm; border-radius: 50%;
      margin-right: 1mm; vertical-align: middle; }}
</style></head><body><div class="sheet">{cards}</div></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("layout")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    layout_dir = subprocess.run([sys.executable, os.path.join(REPO, "tools", "where.py"),
                                 a.layout], capture_output=True, text=True, check=True,
                                cwd=REPO).stdout.strip()
    inv = inventory(layout_dir)
    hostvars = plain(inv.get("_meta", {}).get("hostvars", {}))
    boards = sorted((inv.get("boards") or {}).get("hosts", []))
    groups = {g: set(v.get("hosts", [])) for g, v in inv.items() if isinstance(v, dict)}
    # the registry the layout names (relative to it), else the site's, as the inventory reads it
    try:
        with open(os.path.join(layout_dir, "devreg.yml")) as f:
            named = (yaml.safe_load(f) or {}).get("registry")
    except OSError:
        named = None
    reg_path = os.path.join(layout_dir, os.path.expanduser(named)) if named else site.registry_path()
    try:
        with open(reg_path) as f:
            devices = yaml.safe_load(f)["devices"]
    except OSError:
        sys.exit(f"no device registry at {reg_path}: copy site.example/registry.yaml to the site "
                 "(docs/GETTING-STARTED.md, 'Your site') or name one in the layout's devreg.yml")
    cards = []
    for name in boards:
        hv = hostvars.get(name, {})
        in_groups = [g for g, hosts in groups.items() if name in hosts]
        color = hv.get("bench_led_color") if hv.get("bench_led") else None
        host = next((g[len("host_"):] for g in in_groups if g.startswith("host_")), "")
        cards.append(card(name, devices.get(name) or {}, hv, color, host))
    out = a.out or os.path.join(REPO, "runs", f"labels-{a.layout}.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(PAGE.format(layout=html.escape(a.layout), cards="".join(cards)))
    print(out)


if __name__ == "__main__":
    main()
