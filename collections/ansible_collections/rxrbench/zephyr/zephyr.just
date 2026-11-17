# The rxrbench.zephyr plugin's recipes, imported by the repository's justfile.

# the plugin's example application at its three versions, as images zapp-1.0, zapp-1.1,
# zapp-2.0 (any west workspace with a Zephyr: RXRBENCH_ZEPHYR_WORKSPACE)
[group("simulated benches")]
[doc("the `rxrbench.zephyr` example application at three versions (`zapp-1.0`, `zapp-1.1`, `zapp-2.0`), from any west workspace")]
sim-zapp-build *args:
    #!/usr/bin/env bash
    set -euo pipefail
    z=collections/ansible_collections/rxrbench/zephyr/sim
    for v in 1.0 1.1 2.0; do
        ./venv/bin/python tools/zephyr_sim.py build --name zapp-$v --app $z/zapp --conf $z/zephyr/zapp-$v.conf {{args}}
    done

# the example application for the emulated MCU (the MPS2 AN385 under QEMU, MCUboot in RAM-load
# mode) with a network and Zephyr's hawkBit client, at two versions and a third made to fail: the
# simulated device that fetches its own updates from a server (layout zsimhawkbit). Needs the Zephyr SDK's Arm
# toolchain in the same workspace, and qemu-system-arm to run them.
[group("simulated benches")]
[doc("the `rxrbench.zephyr` example application with a hawkBit client for the emulated MCU with a network (`zqh`, `zqh-b`, and `zqh-bad`, made to fail): the simulated device of the hawkBit route (Zephyr SDK)")]
sim-zapp-qemu-build *args:
    #!/usr/bin/env bash
    set -euo pipefail
    z=collections/ansible_collections/rxrbench/zephyr/sim
    sim=collections/ansible_collections/rxrbench/core/sim/zephyr
    confs="--conf $sim/smp-shell-mcu.conf --conf $z/zapp/hw/qemu-net.conf --conf $z/zapp/hw/hawkbit.conf"
    ./venv/bin/python tools/zephyr_qemu.py build --name zqh --app $z/zapp $confs --conf $z/zephyr/zapp-3.2.conf --family zephyr_settings --version 3.2.0 --network 192.0.2.0/24 {{args}}
    ./venv/bin/python tools/zephyr_qemu.py build --name zqh-b --app $z/zapp $confs --conf $z/zephyr/zapp-3.3.conf --family zephyr_settings --version 3.3.0 --network 192.0.2.0/24 {{args}}
    ./venv/bin/python tools/zephyr_qemu.py build --name zqh-bad --app $z/zapp $confs --conf $z/zephyr/zapp-3.4.conf --family zephyr_settings --version 3.4.0 --network 192.0.2.0/24 {{args}}

# the example application for a real board (sim/zapp/hw/boards.yml: pico2w, picow, kb2040,
# pro-micro-rp2040) at a version (3.0, 3.1), staged with the features it was built with; prints
# its catalog entry (RXRBENCH_ZEPHYR_WORKSPACE; a module the workspace lacks with --module DIR)
[group("plan and apply")]
[doc("the `rxrbench.zephyr` example application for a real board (`sim/zapp/hw/boards.yml`) at a version, staged with its features; prints its catalog entry")]
zapp-build board version *args:
    ./venv/bin/python tools/zapp_build.py {{board}} {{version}} --stage {{args}}
