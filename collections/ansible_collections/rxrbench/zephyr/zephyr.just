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
