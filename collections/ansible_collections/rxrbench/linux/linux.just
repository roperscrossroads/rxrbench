# The Linux plugin's recipes, imported by the repository's justfile when the collection is there.

# start the simulated Linux bench: containers with an ssh server (layout simlinux)
[group("simulated benches")]
[doc("start the simulated Linux bench (layout `simlinux`): containers with an ssh server")]
sim-linux-up *devices:
    ./venv/bin/python collections/ansible_collections/rxrbench/linux/sim/simlinux.py up {{devices}}

# stop it
[group("simulated benches")]
[doc("stop the simulated Linux bench")]
sim-linux-down:
    ./venv/bin/python collections/ansible_collections/rxrbench/linux/sim/simlinux.py down

# a simulated device comes back new (another host key), as a re-imaged one does
[group("simulated benches")]
[doc("a simulated Linux device comes back new (another host key), as a re-imaged one does")]
sim-linux-reimage device:
    ./venv/bin/python collections/ansible_collections/rxrbench/linux/sim/simlinux.py reimage {{device}}

# a Yocto disk image (.wic, with its .bmap) into the image store, for `just card`
[group("plan and apply")]
[doc("a Yocto disk image (`.wic`, with its `.bmap`) into the image store, for `just card`")]
stage-wic wic board_tag family="linux_rauc":
    ./venv/bin/python collections/ansible_collections/rxrbench/linux/tools/stage_wic.py {{wic}} --board-tag {{board_tag}} --family {{family}}

# start the simulated A/B bench: a QEMU machine with RAUC slots (layouts simab, simabrauc);
# needs `simvm.py setup` once (the built image, a U-Boot sandbox binary)
[group("simulated benches")]
[doc("start the simulated A/B device: a QEMU machine with RAUC slots (layouts `simab`, `simabrauc`); needs `simvm.py setup` once")]
sim-vm-up *devices:
    #!/usr/bin/env bash
    set -euo pipefail
    s=collections/ansible_collections/rxrbench/linux/sim/simvm.py
    nohup ./venv/bin/python -u "$s" up {{devices}} > "$HOME/.cache/rxrbench/simvm/simvm.out" 2>&1 &
    ./venv/bin/python "$s" pin {{devices}}

# stop it
[group("simulated benches")]
[doc("stop the simulated A/B device")]
sim-vm-down:
    ./venv/bin/python collections/ansible_collections/rxrbench/linux/sim/simvm.py down

# sign an update bundle for the simulated A/B device from its built image
[group("simulated benches")]
[doc("sign an update bundle for the simulated A/B device from its built image")]
sim-vm-bundle version="1.1-sim":
    ./venv/bin/python collections/ansible_collections/rxrbench/linux/sim/simvm.py bundle --version {{version}}

# build U-Boot's sandbox binary from mainline at the pinned tag (minutes; no vendor tree), then
# `simvm.py setup --sandbox ~/.cache/rxrbench/simvm/sandbox-built`
[group("simulated benches")]
[doc("build U-Boot's sandbox binary from mainline at the pinned tag (minutes), for `simvm.py setup --sandbox`")]
sim-vm-sandbox *args:
    ./venv/bin/python collections/ansible_collections/rxrbench/linux/tools/uboot_sandbox.py build {{args}}
