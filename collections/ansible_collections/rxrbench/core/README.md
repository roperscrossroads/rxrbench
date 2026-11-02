# rxrbench.core

The framework of `rxrbench`: the plays and roles, the `devreg` inventory plugin, the modules
that read and write devices, their shared code, and what a bench host is given. It is
installed from this repository (`ansible.cfg` names `collections/`), not from Galaxy. It holds
no knowledge of one firmware project or board product: that lives in the plugin collections
beside it. How the pieces fit: [docs/ARCHITECTURE.md](../../../../docs/ARCHITECTURE.md);
adding a device family: [docs/PLUGINS.md](../../../../docs/PLUGINS.md).

| Part | What |
|---|---|
| `playbooks/` | the plays the justfile runs: plan and apply, facts, boot, backup and restore (`restore-regions.yml`, `restore-chip.yml`), erase (`erase.yml`, `erase-chip.yml`), secrets, locate, diag, test, the bench hosts |
| `roles/` | `board` (a device to its declared image and config), `bench_host`, `serial_broker`, `default_install` (what a bench host runs), `sim_bench` (a layout's simulated boards on a bench host, each behind a broker) |
| `host/` | what is deployed to a bench host: the serial broker (`broker/`), `bin/` tools (`uf2_flash.py`, `swd_chip.py`, `swd_survey.py`, `chip_blocks.py`, `cp210x_serial.py`, ...), the plug watcher, pinned venv lockfiles |
| `sim/` | the simulated bench: sim boards behind real brokers (`simbench.py`, `simboard.py`), the simulated bench host (`vmhost.py`); their registry and catalog are in `site.example/sim/` |
| `catalog/boards.yml` | the board types that belong to no plugin, by the registry's `board_tag`; a plugin's catalog and a site's are layered over it |
| `plugins/inventory/devreg.py` | a layout directory as an inventory: boards from the device registry, their bench host, routes, family facts and RF rules; the whole layout is refused when it breaks a rule |
| `plugins/modules/` | one module per thing read or written: flashers (`board_flash`, `esp_image_flash`, `picoboot_flash`, `smp_flash`, `swd_flash`, `bootloader_flash`); `card_write`; `board_reboot`; `region_backup`, `region_restore`, `region_erase`; `chip_backup`, `chip_restore`, `chip_erase`; `swd_identify`, `usb_survey`, `device_run` |
| `plugins/module_utils/flash/` | the flashers behind one four-step driver (inspect, refuse, write, read back), and the chip and region flows |
| `plugins/module_utils/` | the rest: layout and layers, RF rules, identity and the state line, the families as data (`familyspec.py`), routes, the write gate, the broker client |
| `plugins/plugin_utils/` | controller only: the built-in families (`families/`), the loader of plugin collections (`registry.py`) |
| `plugins/filter/` | `escrow_get`, `escrow_put`, `secret_verdict` (whether a secret is the one it should be, showing none), `chip_findings` (what a chip holds that the layout does not own) |
| `plugins/lookup/` | `bench_plugins` (the loaded plugin collections and their families as data), `library_tests` (the library tests a layout binds) |
| `plugins/callback/` | `state` (the state a run starts from), `timing` (how long each task and module phase took, per board) |

Every writing module supports check mode and refuses without the layout's go-ahead
(`apply_allowed`); `board_reboot`, which changes no stored state, and `device_run`, which runs
the caller's diagnostics and tests, have no gate. `ansible-doc rxrbench.core.<module>`
documents each, and `just docs` builds the whole reference.
