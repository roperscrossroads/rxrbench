# rxrbench.mesh: building and staging this plugin's firmware. Imported by the repository's
# justfile; recipes run from the repository's directory.

# stage a zephyrtastic build into the image store (manifest.json, flash.json beside the image)
[group("plan and apply")]
[doc("a zephyrtastic build into the image store (`manifest.json`, `flash.json` beside the image)")]
stage-image build board_tag *args:
    ./venv/bin/python collections/ansible_collections/rxrbench/mesh/tools/stage_image.py {{build}} --board-tag {{board_tag}} {{args}}

# build the simulated board (the firmware's own sample app for native_sim) from a firmware
# tree (a directory in the zephyrtastic workspace, catalog `sources:`), as fleet class <class>,
# and stage it in the image store with its manifest
[group("simulated benches")]
[doc("build the mesh firmware's sample app for native_sim from a firmware tree (catalog `sources:`) as fleet class `<class>`, and stage it")]
sim-build tree="main" class="0":
    #!/usr/bin/env bash
    set -euo pipefail
    ws="$(./venv/bin/python tools/where.py --source zephyrtastic workspace)"
    tree="$(cd "$ws/{{tree}}" && pwd)"
    here="{{justfile_directory()}}"
    build="$here/.build/simboard-{{class}}"
    # the workspace's own venv where it has one (a workstation); CI puts west on the PATH
    cd "$ws" && { [ ! -f .venv/bin/activate ] || source .venv/bin/activate; }
    west build -p auto -b native_sim/native/64 -d "$build" "$tree/samples/meshtastic" -- \
        -DZEPHYR_EXTRA_MODULES="$tree" \
        -DEXTRA_CONF_FILE="$here/collections/ansible_collections/rxrbench/mesh/sim/board/sim.conf" \
        -DEXTRA_DTC_OVERLAY_FILE="$here/collections/ansible_collections/rxrbench/mesh/sim/board/sim.overlay" \
        -DCONFIG_MESHTASTIC_FLEET_CLASS={{class}} 2>&1 | tail -3
    grep -qi "meshtastic-zephyr\":\"$tree\"" "$build/zephyr_modules.txt" \
        || { echo "WRONG TREE: the build did not compile $tree" >&2; exit 4; }
    # The manifest says what the image is, in the words the board will use for itself: the
    # build id comes from the header the build generated, not from asking git again.
    id=$(sed -n 's/^#define MESHTASTIC_BUILD_ID "\(.*\)"/\1/p' \
         "$build/modules/meshtastic-zephyr/src/generated/meshtastic_build_id.h")
    store="$here/.build/store/zephyrtastic/sim-{{class}}"
    mkdir -p "$store"
    cp "$build/zephyr/zephyr.exe" "$store/zephyr.exe"
    printf '{"family": "zephyrtastic", "format": "sim-exe", "file": "zephyr.exe", "target": "native_sim", "board_tag": "native-sim", "class": %s, "build": "%s"}\n' \
        "{{class}}" "$id" > "$store/manifest.json"
    echo "sim board class {{class}} built from $tree (build $id) -> $store"

# build zephcore's sample app for native_sim (role: repeater | companion) from its workspace
# (catalog `sources:`), and stage it as zephcore/sim-<role> with its manifest
[group("simulated benches")]
[doc("build zephcore's sample app for native_sim (role `repeater` or `companion`) from its workspace, and stage it")]
sim-build-zephcore role="repeater":
    #!/usr/bin/env bash
    set -euo pipefail
    ws="$(./venv/bin/python tools/where.py --source zephcore workspace)"
    here="{{justfile_directory()}}"
    build="$here/.build/zephcore-sim-{{role}}"
    cd "$ws" && source .venv/bin/activate
    export ZEPHYR_TOOLCHAIN_VARIANT=host; unset ZEPHYR_SDK_INSTALL_DIR
    west build -p auto -b native_sim/native/64 -d "$build" zephcore/samples/{{role}} 2>&1 | tail -2
    # what the image says it is, from the build itself: its build id is a compile define
    id=$(grep -o 'ZEPHCORE_BUILD_ID=[^ ]*' "$build/compile_commands.json" | head -1 | tr -d '\\"' | cut -d= -f2)
    ver=$(sed -n 's/^CONFIG_ZEPHCORE_VERSION="\(.*\)"/\1/p' "$build/zephyr/.config")
    store="$here/.build/store/zephcore/sim-{{role}}"
    mkdir -p "$store"
    cp "$build/zephyr/zephyr.exe" "$store/zephyr.exe"
    printf '{"family": "zephcore", "format": "sim-exe", "file": "zephyr.exe", "target": "native_sim", "board_tag": "native-sim", "role": "%s", "version": "%s", "build": "%s"}\n' \
        "{{role}}" "$ver" "$id" > "$store/manifest.json"
    echo "zephcore {{role}} ($ver, build $id) -> $store"

# start the simulated bench (layouts/sim): sim boards behind real brokers, in the background
[group("simulated benches")]
[doc("start the simulated mesh bench (layout `sim`): sim boards behind real brokers, in the background")]
sim-up *boards="sim1 sim2":
    #!/usr/bin/env bash
    set -euo pipefail
    run="${RXRBENCH_SIM_RUN:-/tmp/rxrbench-sim}"
    mkdir -p "$run"
    if [ -f "$run/simbench.pid" ] && kill -0 "$(cat "$run/simbench.pid")" 2>/dev/null; then
        echo "already up (pid $(cat "$run/simbench.pid"))"; exit 0
    fi
    nohup setsid ./venv/bin/python -u collections/ansible_collections/rxrbench/mesh/sim/simbench.py --run-dir "$run" {{boards}} > "$run/simbench.out" 2>&1 &
    for _ in $(seq 100); do [ -f "$run/simbench.pid" ] && break; sleep 0.1; done
    cat "$run/simbench.out"

# stop the simulated bench; the boards' config stays for the next sim-up
[group("simulated benches")]
[doc("stop the simulated mesh bench; the boards' config stays for the next `sim-up`")]
sim-down:
    #!/usr/bin/env bash
    run="${RXRBENCH_SIM_RUN:-/tmp/rxrbench-sim}"
    [ -f "$run/simbench.pid" ] && kill "$(cat "$run/simbench.pid")" && echo stopped || echo "not up"

# fetch the pinned meshtasticd image the simulated stock Meshtastic bench runs
[group("simulated benches")]
[doc("fetch the pinned meshtasticd image the simulated stock Meshtastic bench runs")]
sim-mt-pull:
    ./venv/bin/python collections/ansible_collections/rxrbench/mesh/sim/stock/simmt.py pull

# start the simulated stock Meshtastic bench (layout simmt): meshtasticd in containers
[group("simulated benches")]
[doc("start the simulated stock Meshtastic bench (layout `simmt`): meshtasticd in containers")]
sim-mt-up *nodes:
    ./venv/bin/python collections/ansible_collections/rxrbench/mesh/sim/stock/simmt.py up {{nodes}}

# stop it (the nodes' config stays for the next sim-mt-up); name nodes to stop only those, and
# --erase to remove their config too (nothing left behind, as the sim test leaves it)
[group("simulated benches")]
[doc("stop the simulated stock Meshtastic bench (the nodes' config stays; `--erase` removes it, nodes may be named)")]
sim-mt-down *args:
    ./venv/bin/python collections/ansible_collections/rxrbench/mesh/sim/stock/simmt.py down {{args}}

# a simulated stock node comes back new: config and identity key gone (a factory reset)
[group("simulated benches")]
[doc("a simulated stock Meshtastic node comes back new: config and identity key gone")]
sim-mt-wipe node:
    ./venv/bin/python collections/ansible_collections/rxrbench/mesh/sim/stock/simmt.py wipe {{node}}

# read a stock Meshtastic node on the network (TCP 4403): owner, device, display, channels as
# fingerprints, admin keys; changes nothing (the module in check mode, nothing declared)
[group("plan and apply")]
[doc("read a stock Meshtastic node on the network (TCP 4403): owner, device, display, channels as fingerprints, admin keys; changes nothing")]
net-facts host port="4403":
    #!/usr/bin/env bash
    set -euo pipefail
    ANSIBLE_STDOUT_CALLBACK=json ./venv/bin/ansible localhost -m rxrbench.mesh.meshtastic_net_config \
        -a 'host={{host}} port={{port}}' --check 2>&1 | cat
