#!/usr/bin/env python3
"""zapp_build.py <board> <version> [--workspace W] [--module DIR]... [--stage]: build the zephyr plugin's example
application, zapp, for a real board, and stage it in the image store with what it provides.

<board> is a row of collections/ansible_collections/rxrbench/zephyr/sim/zapp/hw/boards.yml (its
Zephyr target, board type, confs, overlays and modules); <version> one of sim/zephyr/zapp-<v>.conf
(3.0, 3.1). The build is .build/zapp/<board>-<version>/; with --stage, its .uf2 goes to the store
as `zapp-<version>-<board>` (tools/stage_raw.py: the flash map from the build, the version, and
the features the build listed in zapp-features.txt), and the catalog entry for a site's
catalog/firmware.yml is printed.

W is a west workspace holding a Zephyr (default RXRBENCH_ZEPHYR_WORKSPACE); its `.venv/bin/west`
is used when there is one. A module a board needs that the workspace does not hold
(hal_rpi_pico, hal_infineon) is given with --module DIR, a directory named as the module.
"""
import argparse
import os
import subprocess
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zephyr_sim  # noqa: E402  pylint: disable=wrong-import-position

ZAPP = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "zephyr", "sim", "zapp")
HW = os.path.join(ZAPP, "hw")
VERSIONS = os.path.join(os.path.dirname(ZAPP), "zephyr")
BUILDS = os.path.join(REPO, ".build", "zapp")


def boards():
    with open(os.path.join(HW, "boards.yml")) as f:
        return yaml.safe_load(f)


def module_dirs(workspace, names, extra):
    """Each named module's directory: the workspace's modules/hal/<name>, else the one of
    `extra` (the --module directories) named so."""
    out = []
    for name in names:
        if os.path.isdir(os.path.join(workspace, "modules", "hal", name.removeprefix("hal_"))):
            continue   # the workspace's own: west finds it
        found = [p for p in extra if os.path.basename(os.path.normpath(p)) == name]
        if not found:
            sys.exit(f"zapp_build: {name} is not in {workspace}/modules/hal: give its directory "
                     f"with --module DIR")
        out.append(os.path.abspath(os.path.expanduser(found[0])))
    return out


def command(west, row, version, build_dir, modules):
    """The west build line for one board and version."""
    confs = [os.path.join(HW, "hw.conf")] + [os.path.join(HW, c) for c in row.get("confs") or []] + \
        [os.path.join(VERSIONS, f"zapp-{version}.conf")]
    cmd = [west, "build", "-p", "always", "-b", row["target"], "-S", "cdc-acm-console", "-d", build_dir, ZAPP,
           "--", "-DEXTRA_CONF_FILE=" + ";".join(confs)]
    if row.get("overlays"):
        cmd.append("-DEXTRA_DTC_OVERLAY_FILE=" + ";".join(os.path.join(HW, o) for o in row["overlays"]))
    if modules:
        cmd.append("-DZEPHYR_EXTRA_MODULES=" + ";".join(modules))
    return cmd


def catalog_entry(name, row, version, features):
    return {name: {"family": "zephyr_settings",
                   "features": features,
                   "summary": f"zapp {version} ({' '.join(features)}), built by tools/zapp_build.py",
                   "board_tags": [row["board_tag"]], "flasher": "picoboot",
                   "source": {"type": "path", "path": f".build/store/zephyr_settings/{name}"}, "rf": "none"}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board")
    ap.add_argument("version")
    ap.add_argument("--workspace", help="the west workspace (default RXRBENCH_ZEPHYR_WORKSPACE)")
    ap.add_argument("--module", action="append", default=[], metavar="DIR",
                    help="a Zephyr module the workspace lacks (a directory named as the module)")
    ap.add_argument("--stage", action="store_true", help="stage the image and print its catalog entry")
    a = ap.parse_args(argv)
    table = boards()
    if a.board not in table:
        sys.exit(f"zapp_build: no board {a.board!r} in hw/boards.yml ({', '.join(table)})")
    if not os.path.isfile(os.path.join(VERSIONS, f"zapp-{a.version}.conf")):
        sys.exit(f"zapp_build: no sim/zephyr/zapp-{a.version}.conf")
    workspace = os.path.abspath(os.path.expanduser(a.workspace or os.environ.get("RXRBENCH_ZEPHYR_WORKSPACE") or ""))
    if not os.path.isdir(os.path.join(workspace, "zephyr")):
        sys.exit("zapp_build: no workspace: --workspace W, or RXRBENCH_ZEPHYR_WORKSPACE=W (a west workspace)")
    row = table[a.board]
    build_dir = os.path.join(BUILDS, f"{a.board}-{a.version}")
    cmd = command(zephyr_sim.west_of(workspace), row, a.version, build_dir,
                  module_dirs(workspace, row.get("modules") or [], a.module))
    print("+", " ".join(cmd), flush=True)
    if subprocess.run(cmd, cwd=workspace, check=False).returncode:
        sys.exit("zapp_build: west build failed")
    uf2 = os.path.join(build_dir, "zephyr", "zephyr.uf2")
    with open(os.path.join(build_dir, "zapp-features.txt")) as f:
        features = f.read().split()
    print(f"zapp {a.version} for {a.board}: {uf2}; features {' '.join(features)}")
    if not a.stage:
        return 0
    name = f"zapp-{a.version}-{a.board}"
    stage = [sys.executable, os.path.join(REPO, "tools", "stage_raw.py"), uf2, "--name", name,
             "--board-tag", row["board_tag"], "--family", "zephyr_settings", "--target", row["target"],
             "--version", a.version, "--flash-map", build_dir, "--features", ",".join(features)]
    if subprocess.run(stage, cwd=REPO, check=False).returncode:
        sys.exit("zapp_build: staging failed")
    print("# for the site's catalog/firmware.yml, under images:")
    print(yaml.safe_dump(catalog_entry(name, row, a.version, features), sort_keys=False, default_flow_style=None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
