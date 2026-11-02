# Where everything lives

Every file rxrbench reads or writes, on which machine, and what moves it. Two machines take
part: the **workstation**, where `just` runs, and each **bench host**, where boards are plugged
in. One machine can be both; then both lists apply to it.

Paths in the checkout are relative to it. `~` is the home of the user that runs the tool (on a
bench host, the layout's `ansible_user`).

## The workstation: the checkout

Everything but the tracked tool is ignored by git.

| Path | What | Written by |
|---|---|---|
| `collections/` | the tool: the core collection and the bundled plugins (tracked) | git |
| `venv/` | the tool's Python venv (Ansible, test tools, the plugins' libraries) | `just setup` |
| `.collections/` | third-party Ansible collections (`community.sops`) | `just setup` |
| `site/` | the site: your bench's configuration (below); `RXRBENCH_SITE` moves it | you, `just survey`, `just enroll` |
| `.env` | your settings, `NAME=value` per line, read by every recipe (`.env.example` lists them); the environment wins | you |
| `.build/store/<family>/<image>/` | staged images (image files, `manifest.json`, `flash.json`); layouts name it as `bench_store` | `just stage-image`, the sim builds |
| `.build/debug/<family>/<image>/` | the ELF of a staged image, for `just debug` | `just stage-image` |
| `.build/zephyr/`, the plugins' `.build/<name>-*` | build directories of the simulated boards | the `sim-*-build` recipes |
| `.build/docs/`, `.build/docs-venv/` | the generated reference and its tools' venv | `just docs` |
| `backups/<board>/...` | region and chip backups fetched from the bench hosts (below); can hold private keys | `just backup`, `just snapshot` |
| `runs/<run>/` | the record of a plan, apply, test, diagnosis or scenario run (`<run>` a UTC timestamp): per-board results, `state.json` (what a `just run` tested against), `timing-<play>.json` (read by `just timing`); `-e bench_runs_dir=` moves it | `just plan`, `apply`, `device-test`, `diag`, `scenario`, `run` |

## The workstation: the site

The site is one bench's configuration: `site/`, or the directory `RXRBENCH_SITE` names (relative
to the checkout). Keep it as its own private git repository; start from a copy of
`site.example/`, including its `catalog/` (some board types and their keys live only there).
Its shape: `site.example/README.md`.

| Path | What | Override |
|---|---|---|
| `registry.yaml` | the device registry ([REGISTRY.md](REGISTRY.md)) | `DEVREG_REGISTRY`; a layout's `registry:` |
| `facts/<name>.yaml` | what the survey read from each device; `new-<vid><pid>-<id>.yaml` until enrolled | `RXRBENCH_FACTS`; `RXRBENCH_FACTS_COMMIT=1` commits them |
| `layouts/<layout>/` | a bench you declare ([LAYOUT.md](LAYOUT.md)); `layouts/hosts/hosts.yml` names the bench hosts | looked for in the site, then `site.example/layouts/` |
| `layers/` | declarations several layouts share | the site, then `site.example/layers/` |
| `tests/<name>.yml` | library tests, bound to boards by a layout's `bench_test_bindings` | the site the layout lives in |
| `campaigns/<name>.yml` | states applied and tested in turn (`just campaign`) | the site, then `site.example/campaigns/` |
| `rf_rules.yml` | where the site is and what it may transmit | a layout's `rf_ceiling:`; its `rf_rules:` only tightens |
| `plugins.yml` | the plugin collections the site loads ([PLUGINS.md](PLUGINS.md)) | `RXRBENCH_PLUGINS` (comma-separated) |
| `collections/` | the site's own plugin collections | Ansible looks only in `./site/collections`: with the site elsewhere, add its `collections/` to `ANSIBLE_COLLECTIONS_PATH` |
| `catalog/firmware.yml`, `boards.yml` | the site's images and board types, over the tool's and the plugins' | |
| `host-files.yml` | extra files the site puts on its bench hosts | |
| `.sops.yaml` | which age key encrypts the site's secrets | |
| `identity/<board>.sops.yaml` | escrowed device identities, encrypted | |
| `golden/`, `private-patterns.txt` | recorded outputs for `just golden`; the site's own patterns for `just privacy` | |

## The workstation: its home

| Path | What | Written by | Override |
|---|---|---|---|
| `~/.local/share/rxrbench/` | the pinned tools (`sops`, `age`, ...): `cache/`, `pkg/`, `bin/` (recipes add `bin/` to the end of `PATH`) | `just setup`, `just pins install` | |
| `~/.local/state/rxrbench/plug-<host>.json` | the plug relay's last reading of each bench host, and the location index | the plug relay | `RXRBENCH_LOCATION_DIR` |
| `~/.config/systemd/user/rxrbench-plug-relay.{service,timer}` | the plug relay, every 30 s | `just hosts-install` | |
| `~/.cache/rxrbench/artifacts/` | firmware fetched from a catalog URL, by sha256 | a play that stages a remote image | `XDG_CACHE_HOME` |
| `~/.cache/rxrbench/launcher/`, `~/.config/rxrbench/launcher.auth` | Launcher web sessions; its password file | `rxrbench.launcher` | `XDG_CACHE_HOME`, `XDG_CONFIG_HOME` |
| `~/.cache/rxrbench/vmhost/` | the simulated bench host (image, ssh key, run state) | `just sim-host-setup` | `RXRBENCH_VMHOST_DIR` |
| `~/.cache/rxrbench/simvm/`, `simlinux/` | the simulated A/B device and Linux containers | `just sim-vm-*`, `sim-linux-*` | |
| `~/.config/sops/age/keys.txt` | your age private key | you (`age-keygen`) | `SOPS_AGE_KEY_FILE` |
| `/tmp/rxrbench-sim/` | the simulated mesh bench's sockets, logs and pid | `just sim-up` | `RXRBENCH_SIM_RUN` |

## A bench host

Everything is under the user's home except the runtime directory. A role variable moves a path
for a host or a layout (`bench_host_*`, `roles/bench_host/defaults`).

| Path | What | Written by |
|---|---|---|
| `~/.devreg/` | the host's copy of `registry.yaml`, `devreg.py`, `devlock.py`: every write checks a port against it | `just hosts-setup`, `just enroll` |
| `~/.venv-<name>/` | one venv per tool family (`esp`, `bench`, `mcumgr`, `swd`, and each plugin's, such as `mesh`) | `just hosts-install` |
| `~/.local/share/rxrbench/` | the pinned tools (`uv`, `probe-rs`, ...) and the venvs' lockfiles | `just hosts-install` |
| `~/rxrbench/bin/` | the host tools (flashers, survey probes, `brokerctl.py`), core's and each plugin's | `just hosts-setup` |
| `~/rxrbench/catalog/` | `boards.yml`, the board types of the tool, the plugins and the site merged, which the host tools read (`catalog/site/boards.yml` too, when present; `UF2_BOARDS` overrides both) | `just hosts-setup` |
| `~/rxrbench/lib/` | the serial broker (its own `venv/`, `dialects.d/` from the plugins) and the plug watcher | `just hosts-setup`, `just hosts-install` |
| `~/rxrbench/store/<image>/` | images copied here before a flash | `just apply`, `boot`, `restore-regions`, ... |
| `~/rxrbench/backups/<board>/` | backups, before they are fetched to the workstation | `just backup`, `snapshot` |
| `~/rxrbench/firmware/` | the plugins' pinned downloads (each plugin's `host/host.yml`, by sha256), such as a bootloader update; `firmware/backups/` holds what such an update read back before writing | `just hosts-setup`; that update |
| `~/rxrbench/logs/<board>.log` | each serial broker's log; `openocd-<board>.log` from `just debug` | the broker, `just debug` |
| `~/rxrbench/state/usb-present.json`, `plug-events.jsonl` | the plug watcher's snapshot of the USB devices (with the `chip_id` of a board it identified in a bootloader that hides it) and its event log | the plug watcher |
| `~/rxrbench/state/last-read/<board>.json` | each board's last full identity read, which a plan at `spot` depth may use | the flashers |
| `~/rxrbench/state/busy/<name>.json` | a busy mark: a board is being flashed; the watcher and survey leave its port alone, the relay holds its last reading | the flashers, picoboot chip sessions |
| `~/rxrbench/sim/` | the simulated boards on a simulated bench host | `just hosts-setup` (a sim layout) |
| `~/rxrbench/bench/` | empty; for the site's own files (`host-files.yml`) | `just hosts-setup` |
| `~/.config/systemd/user/` | `rxrbench-serial@.service` (one broker per console board), `rxrbench-plugwatch.service`, `rxrbench-simbench.service` | `just hosts-setup`, `just hosts-install` |
| `~/.config/rxrbench/serial/<board>.env` | a board's broker settings (`RXRBENCH_MODEM_REST`: how DTR/RTS rest) | `just hosts-setup` |
| `/run/user/<uid>/rxrbench/<board>.sock` | each broker's socket (`bench_socket` overrides) | the broker |
| `/run/user/<uid>/devlock/` | device locks: one tool at a time per port (`DEVLOCK_DIR`) | every tool that opens a port |
| `/tmp/uf2_flash-<serial>` | a UF2 drive's mount point during a flash | the UF2 flashers (with sudo) |

**The state directory is fixed.** `RXRBENCH_HOST_STATE` moves it for the flashers, the last-read
records and a plug relay reading this same machine, but the plug watcher always writes
`~/rxrbench/state`, and the relay reads `~/rxrbench/state` on a remote host. On a real bench
host leave it unset; it exists for the tests.

## Firmware, from source to board

1. A build is **staged** on the workstation into `.build/store/<family>/<image>/`, or named in a
   catalog by URL and sha256 and fetched into `~/.cache/rxrbench/artifacts/` when a play needs it.
2. A layout names the image (`bench_image`, under `bench_store`).
3. `just apply` (or `boot`, `restore-regions`, ...) copies it to the bench host's
   `~/rxrbench/store/<image>/` and flashes from there. A simulated board on the workstation reads
   it in place.

Nothing a flash overwrites is kept, except by a plugin's bootloader update that reads back
first. Take a `just backup` or `just snapshot` first when you want to go back.

## Backups

| What | On the bench host | Fetched to the workstation |
|---|---|---|
| config regions (`just backup`) | `~/rxrbench/backups/<board>/<stamp>-<build>/` | `backups/<board>/<stamp>/` |
| the whole chip (`just backup`, `just snapshot`) | `~/rxrbench/backups/<board>/chip/<stamp>/` | `backups/<board>/chip/<stamp>/` (`meta.json`, `flash.bin.gz`); `backups/<board>/snapshot/<name>` |
| a CP2102 bridge's descriptor (`just cp210x-serial ... --backup FILE`) | the file you name | |

`backups/` can hold device keys: keep it with the site's other private data. An SD card write
(`just card`) erases the card and keeps nothing.

## Secrets

| What | Where | How |
|---|---|---|
| channel keys, passwords | a layout's `group_vars/*.sops.yaml`, `host_vars/<board>.sops.yaml` | sops-encrypted; decrypted in memory at run time by the `community.sops` vars plugin |
| escrowed device identities | `<site>/identity/<board>.sops.yaml` (0600, in a 0700 directory) | written encrypted by `just keys-export`; read in memory by `keys-restore` and apply |
| which key encrypts | `<site>/.sops.yaml` | from `.sops.yaml.example` |
| the key that decrypts | `~/.config/sops/age/keys.txt` on the workstation | never on a bench host: nothing is decrypted there |

Secrets are decrypted only on the workstation and sent to a board over its own connection. The
repository's ignore rules keep `*.sops.yaml`, `.sops.yaml`, `*.key`, `*.pem` and `.env*` out of
it wherever they land.

## Settings that move things

Read from the environment or `.env` (the environment wins).

| Variable | Default | Moves |
|---|---|---|
| `RXRBENCH_SITE` | `site` | the site |
| `DEVREG_REGISTRY` | `<site>/registry.yaml` (bench host: `~/.devreg/registry.yaml`) | the registry |
| `RXRBENCH_FACTS` | `<site>/facts` | the survey facts |
| `RXRBENCH_PLUGINS` | `<site>/plugins.yml` | which plugins load |
| `RXRBENCH_LOCATION_DIR` | `~/.local/state/rxrbench` | the plug relay's state and the location index |
| `RXRBENCH_HOST_STATE` | `~/rxrbench/state` | see "The state directory is fixed" above |
| `RXRBENCH_SIM_RUN` | `/tmp/rxrbench-sim` | the simulated mesh bench's run directory |
| `RXRBENCH_VMHOST_DIR` | `~/.cache/rxrbench/vmhost` | the simulated bench host |
| `RXRBENCH_ZEPHYR_WORKSPACE`, `RXRBENCH_NUTTX_WORKSPACE`, `ZEPHYR_SDK_INSTALL_DIR` | none | where simulated boards are built ([SIMULATED.md](SIMULATED.md)) |
| `DEVLOCK_DIR` | `$XDG_RUNTIME_DIR/devlock` | the device locks |
| `XDG_CACHE_HOME`, `XDG_CONFIG_HOME` | `~/.cache`, `~/.config` | the download cache, Launcher's sessions and password |
| `SOPS_AGE_KEY_FILE` | `~/.config/sops/age/keys.txt` | the age key |

Read the same way, but not paths: `RXRBENCH_BENCH_NTFY`, `RXRBENCH_BUS_SEND`,
`RXRBENCH_BUS_SENDER` (plug notifications), `RXRBENCH_FACTS_COMMIT`, `RXRBENCH_OWNER`,
`RXRBENCH_CARD_KIT`, `RXRBENCH_GDB`, `RXRBENCH_OPENOCD`, `USBIPD`. `.env.example` describes each.
