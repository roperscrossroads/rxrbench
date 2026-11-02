# The rxrbench.launcher plugin's recipes, imported by the repository's justfile.

# an ESP32's Launcher WebUI: `just launcher --board cyd2 status`, `just launcher --board cyd2 install x.bin`, ...
# (--board resolves the registry's wifi route through devreg; --url http://host reads only)
[group("bench hosts and devices")]
[doc("an ESP32's Launcher WebUI (`--board <name> status`, `install x.bin`, ...); `--board` resolves the registry's wifi route through devreg, `--url` reads only")]
launcher *args:
    ./venv/bin/python collections/ansible_collections/rxrbench/launcher/tools/launcherctl.py {{args}}
