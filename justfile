# rxrbench: declare the bench, plan it, apply it.
# Naming: a recipe's first argument is a LAYOUT (plan, apply, facts, diag, ...), except the
# hosts-* recipes (bench hosts and the workstation themselves), wsl-* (usbipd on a WSL
# workstation), and the tool's own (setup, test, privacy, docs*, sim-*, enroll).
# Boards at a time: backup and keys-* take 4 by default, apply 1; `-e parallel=N` changes it
# (a board's broker serves one client at a time; a second one waits its turn).
# A recipe that writes outside apply (boot, restore, restore-chip, switch, keys-restore, erase,
# erase-chip) is a plan unless the word `confirm=apply` is among its arguments: `just erase lab
# b1 storage` says what would be erased, `just erase lab b1 storage confirm=apply` erases it.
# Recipes pipe Ansible through `cat` (it refuses a non-blocking stderr); pipefail keeps its
# exit status, so a failed run fails the recipe.
set shell := ["bash", "-o", "pipefail", "-c"]
# Site settings (paths, URLs): .env, gitignored; .env.example lists them.
set dotenv-load := true
# The tools rxrbench pins (core/host/binaries.yml: sops, age, uv, probe-rs) are found after the
# user's own: their bin/ goes at the end of PATH.
export PATH := env("PATH") + ":" + env("HOME") + "/.local/share/rxrbench/bin"

# A layout by name: the site's (RXRBENCH_SITE, default site/), else site.example/.
lay := "./venv/bin/python tools/where.py"

ap := "./venv/bin/ansible-playbook"
# the plays are the core collection's
pb := "collections/ansible_collections/rxrbench/core/playbooks"

# What a plugin collection adds to these recipes (building and staging its firmware): its own
# justfile, when the collection is there.
import? 'collections/ansible_collections/rxrbench/mesh/mesh.just'
import? 'collections/ansible_collections/rxrbench/linux/linux.just'
import? 'collections/ansible_collections/rxrbench/zephyr/zephyr.just'
import? 'collections/ansible_collections/rxrbench/nuttx/nuttx.just'
import? 'collections/ansible_collections/rxrbench/launcher/launcher.just'

# list recipes
default:
    @just --list

# what this machine has of what the simulated benches need (docs/GETTING-STARTED.md#requirements); `--require
# zephyr,nuttx` makes the exit code say whether those can run
[group("the tool")]
[doc("what this machine has of what the simulated benches need, and what provides a missing one; `--require zephyr,nuttx` makes the exit code say whether those can run")]
doctor *args:
    ./venv/bin/python tools/doctor.py {{args}}

# the venv and the collections, sops and age (pinned), the git hooks
[group("the tool")]
[doc("the tool's venv, the collections, sops and age (pinned), the git hooks")]
setup:
    python3 -m venv venv
    ./venv/bin/pip install -q -r requirements.txt
    for r in collections/ansible_collections/*/*/requirements.txt; do ./venv/bin/pip install -q -r "$r"; done
    ./venv/bin/ansible-galaxy collection install -r requirements.yml -p .collections
    ./venv/bin/python tools/pins.py install sops age
    git config core.hooksPath hooks

# the pinned single-file tools on this machine: pins list | install [name ...] | path
[group("the tool")]
[doc("the pinned single-file tools on this machine: `list`, `install [name ...]`, `path` (`core/host/binaries.yml`, `docs/GETTING-STARTED.md#requirements`)")]
pins *args:
    ./venv/bin/python tools/pins.py {{args}}

# the hosts and groups a layout resolves to (read-only, no bench contact)
[group("layouts, read-only")]
[doc("the hosts and groups a layout resolves to")]
inventory layout:
    l="$({{lay}} {{layout}})" && ./venv/bin/ansible-inventory -i "$l" --graph 2>&1 | cat

# one board's declared state with the file that set each value; a dotted key narrows it
# (`--ansible` also checks the result against ansible-inventory). Read-only, no bench contact.
[group("layouts, read-only")]
[doc("one board's declared state, each value with the file that set it; a dotted key narrows it (`--ansible` also checks the result against ansible-inventory)")]
show layout board *args:
    ./venv/bin/python tools/show.py {{layout}} {{board}} {{args}}

# the layer stack a layout inherits from, lowest first, and what each layer brings
[group("layouts, read-only")]
[doc("the layers a layout inherits, lowest first, and what each brings")]
layers layout:
    ./venv/bin/python tools/show.py {{layout}} --layers

# what every layout resolves to, as files: `record` writes the site's to <site>/golden/
# (`--example`: the examples to tests/golden/), `check` diffs the layouts against the record.
# Run check after any change to the inventory plugin, the layers, the families or the roles.
[group("layouts, read-only")]
[doc("what every layout resolves to, as files: `record` writes it (`--example`: the examples, to `tests/golden/`), `check` diffs the layouts against it; run `check` after a change to the inventory plugin, the layers, the families or the roles")]
golden action *args:
    ./venv/bin/python tools/golden.py {{action}} {{args}}

# everything Ansible sees for a layout, as YAML (a before/after record for a migration)
[group("layouts, read-only")]
[doc("everything Ansible sees for a layout, as YAML (a before/after record for a migration)")]
dump layout:
    l="$({{lay}} {{layout}})" && ./venv/bin/ansible-inventory -i "$l" --list --yaml 2>&1 | cat

# bring a layout's bench hosts up to date (add --check --diff to see what would change)
[group("bench hosts and devices")]
[doc("a layout's bench hosts up to date: devreg, the registry, the serial brokers (`--check --diff` shows what would change)")]
hosts-setup layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/host.yml {{args}} 2>&1 | cat

# the default install on a bench host or the workstation (-K when its sudo asks a password)
[group("bench hosts and devices")]
[doc("the default install on a bench host or the workstation: tools, venvs, the plug watcher or relay (`-K` when its sudo asks a password)")]
hosts-install host *args:
    l="$({{lay}} hosts)" && {{ap}} -i "$l" {{pb}}/install.yml --limit {{host}} {{args}} 2>&1 | cat

# register a device the survey found: registry entry from its facts, facts filed under its
# name, registry pushed to the bench hosts; --layout L --key V also adds it to a layout
[group("bench hosts and devices")]
[doc("register a device the survey found, from its facts, and push the registry to the bench hosts; `--layout L --family F (--key V | --image I)` also declares it in a layout")]
enroll name ident *args:
    ./venv/bin/python tools/enroll.py {{name}} {{ident}} {{args}}

# a CP2102 bridge on a bench host: show <port> | set <port> <new> --expect <cur> --backup <file>
# (as root there; it never writes the lock)
# Each argument reaches the remote command as one word, whatever it contains.
[positional-arguments]
[group("bench hosts and devices")]
[doc("read or set a CP2102 bridge's serial on a bench host")]
cp210x-serial host *args:
    host="$1"; shift; ssh "$host" "sudo \"\$HOME/.venv-swd/bin/python\" \"\$HOME/rxrbench/bin/cp210x_serial.py\"${@:+$(printf ' %q' "$@")}"

# the device registry, from this checkout: devreg list | scan | resolve <name> | guard <name>
# <path> | where | lint ... (devreg/README.md); it reads the site's registry
[positional-arguments]
[group("bench hosts and devices")]
[doc("the device registry, from this checkout, against the site's registry: `list`, `scan`, `resolve <name>`, `guard <name> <path>` (`devreg/README.md`)")]
devreg *args:
    python3 devreg/devreg.py "$@"

# the feature index: each collection's features.yml, checked against the code (every family,
# verb, flasher and route has a feature; every path and test exists); --gaps, --list, --owners <path>
[group("the tool")]
[doc("the feature index (each collection's `features.yml`): checked against the code by default; `--gaps` what is unproven, `--list` every feature, `--owners <path>...` the features a file belongs to")]
features *args:
    ./venv/bin/python tools/features.py {{ if args == "" { "--check" } else { args } }}

# core alone: the unit tests against a tree with core and a fixture plugin only
# (docs/ARCHITECTURE.md#core-portability)
[group("the tool")]
[doc("the unit tests against core alone: a collections tree with `rxrbench.core` and a fixture plugin, no bundled plugin (`docs/ARCHITECTURE.md#core-portability` P1)")]
core-only *args:
    ./venv/bin/python tools/core_only.py {{args}}

# refuse site-private text in the tool's tracked files (CONTRIBUTING.md#public-and-private)
[group("the tool")]
[doc("the privacy check alone: refuse site-private text in the tool's tracked files (`CONTRIBUTING.md#public-and-private`)")]
privacy:
    ./venv/bin/python tools/privacy_check.py

# the plan for a layout's bootloaders (it resets nothing; -e boot_read=true reads the boards,
# which resets an ESP32); confirm=apply writes them
[group("plan and apply")]
[doc("bootloaders: the plan, or with `confirm=apply` the write; a plan resets nothing, it decides from the bootloader last read (`-e boot_read=true` reads, which resets an ESP32)")]
boot layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/boot.yml {{ if args =~ '(^| )confirm=apply( |$)' { replace(args, "confirm=apply", "") } else { "--check --diff " + args } }} 2>&1 | cat

# every USB device on a bench host: identify, match to the registry, store facts and tags
# (deep=true also resets ESP32 boards through esptool/espefuse for chip, flash, PSRAM, eFuses)
[group("bench hosts and devices")]
[doc("identify every USB device on a bench host: facts and tags per device (`deep=true` also resets ESP32 boards for chip, flash, PSRAM and eFuse facts); `RXRBENCH_FACTS_COMMIT=1` ends each run with one `chore(facts)` commit")]
survey host="all" deep="false" *args:
    l="$({{lay}} hosts)" && {{ap}} -i "$l" {{pb}}/survey.yml --limit {{host}} -e survey_deep={{deep}} {{args}} 2>&1 | cat

# read every board's actual state; changes nothing
[group("plan and apply")]
[doc("read every board's actual state; changes nothing")]
facts layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/facts.yml {{args}} 2>&1 | cat

# on a WSL workstation: attach this workstation's registry boards through usbipd (or the named
# ones); the workstation is the registry host devreg takes for this machine (tools/usbipd_attach.py)
[group("bench hosts and devices")]
[doc("on a WSL workstation: attach the registry's workstation boards through usbipd")]
wsl-attach *names:
    ./venv/bin/python tools/usbipd_attach.py {{names}}

# what applying a layout would change; changes nothing
[group("plan and apply")]
[doc("what applying a layout would change; changes nothing")]
plan layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/apply.yml --check --diff -e "bench_run=$(date -u +%Y%m%dT%H%M%SZ)" {{args}} 2>&1 | cat

# bring a layout's boards to their declared state
[group("plan and apply")]
[doc("bring a layout's boards to their declared state: image, radio, config (`--limit <board>`)")]
apply layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/apply.yml --diff -e "bench_run=$(date -u +%Y%m%dT%H%M%SZ)" {{args}} 2>&1 | cat

# back up each board at the levels it has: its config regions (from the running image's flash
# map, for a family that has them) and its whole chip, ESP32 or RP2040/RP2350, on its bench host
# and in backups/ here (each board reboots once per level; -e chip=false: regions only)
[group("backups and secrets")]
[doc("region backups (the config regions the running image's flash map names) and chip backups of a layout's ESP32, RP2040/RP2350 and probe-wired boards, in `backups/` here (`-e chip=false`: regions only); a board with no backup path is refused unless `-e partial=true`")]
backup layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/backup.yml {{args}} 2>&1 | cat

# write one board's config regions back from a region backup (default: its newest); the board
# reboots once and comes back as the node it was. A plan (every check, nothing written) unless confirm=apply
[group("backups and secrets")]
[doc("write a region backup back (the regions where the firmware keeps its config): the board reboots once and comes back as the node it was; a plan unless `confirm=apply`")]
restore-regions layout board backup="latest" *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/restore-regions.yml --limit {{board}} -e restore_from={{ if backup == "confirm=apply" { "latest" } else { backup } }} {{ if backup + " " + args =~ '(^| )confirm=apply( |$)' { replace(args, "confirm=apply", "") } else { "--check --diff " + args } }} 2>&1 | cat

# JTAG on an ESP32-S3 board over its console's USB cable: start | attach [gdb -ex ...] | stop | status
# (attach loads the symbols of exactly the image the board runs, and resumes it at the end)
[positional-arguments]
[group("plan and apply")]
[doc("JTAG on an ESP32-S3 over its console's USB cable: `start`, `attach [gdb args]` (the symbols of exactly the image it runs), `stop`, `status`")]
debug layout board action="status" *gdb_args:
    ./venv/bin/python tools/debug.py "$@"

# a printable sheet of board labels for a layout (name, type, serial tail, host, LED colour)
[group("layouts, read-only")]
[doc("a printable sheet of board labels for a layout (name, type, serial tail, host, LED colour)")]
labels layout:
    ./venv/bin/python tools/labels.py {{layout}}

# blink one board's LED so it can be found (white on a board with a colour); 0 stops it
[group("plan and apply")]
[doc("blink one board's LED so it can be found (white on a board with a colour); 0 seconds stops it")]
locate layout board seconds="60" *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/locate.yml --limit {{board}} -e locate_seconds={{seconds}} {{args}} 2>&1 | cat

# reboot one board through its console and wait until it answers again; writes nothing
# (add --check to read the board and send nothing)
[group("plan and apply")]
[doc("reboot one board through its console and wait until it answers again (`--check` reads it and sends nothing)")]
reboot layout board *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/reboot.yml --limit {{board}} {{args}} 2>&1 | cat

# A non-root `runner` user with passwordless sudo, as on GitHub (tools/ci/Dockerfile), on a clean
# clone of HEAD (commit first), with the host's Docker (the container sims publish on its
# loopback ports).
# run a GitHub Actions job (and the jobs it needs) here with act, as GitHub runs it
[group("the tool")]
[doc("a GitHub Actions job (default `containers`) and the jobs it needs, run here with `act` as GitHub runs it: a non-root user with sudo, on a clean clone of HEAD (needs `act` and Docker)")]
ci job="containers":
    #!/usr/bin/env bash
    set -euo pipefail
    # a clone, not a worktree: a worktree's .git points back into this repository, which the
    # job container does not see
    tmp="$(mktemp -d)"; wt="$tmp/rxrbench"
    git clone -q --no-hardlinks . "$wt" && git -C "$wt" checkout -q --detach "$(git rev-parse HEAD)"
    trap 'rm -rf "$tmp"' EXIT
    docker build -q -t rxrbench-act-runner --build-arg UID="$(id -u)" tools/ci >/dev/null
    act -C "$wt" -j {{job}} -P ubuntu-24.04=rxrbench-act-runner --pull=false --bind --network host \
        --env HOME=/home/runner --env USER=runner --env LOGNAME=runner \
        --container-options "--user runner --group-add $(stat -c %g /var/run/docker.sock)"

# stage a catalogued image from its published source (fetched, sha256-checked) into the image store
[group("plan and apply")]
[doc("a catalogued image from its published source (`github` or `url`, sha256-pinned, a zip `member`) into the image store, with the version and build it reports")]
stage image *args:
    ./venv/bin/python tools/stage_catalog.py {{image}} {{args}}

# a named snapshot of a layout's ESP32 and RP2040/RP2350 boards (each whole chip, filed under <name>); writes nothing;
# a layout with other boards is refused, naming them, unless -e partial=true
[group("backups and secrets")]
[doc("a named whole-chip snapshot of a layout's ESP32 and RP2040/RP2350 boards; other boards are refused unless `-e partial=true`")]
snapshot layout name *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/snapshot.yml -e snapshot_name={{name}} {{args}} 2>&1 | cat

# put a layout's ESP32 and RP2040/RP2350 boards back to a named snapshot, writing only the blocks that differ;
# a plan unless confirm=apply; other boards refused as for snapshot (-e partial=true leaves them out)
[group("backups and secrets")]
[doc("put a layout's boards back to a named snapshot; a plan unless `confirm=apply`")]
switch layout name *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/switch.yml -e snapshot_name={{name}} {{ if args =~ '(^| )confirm=apply( |$)' { replace(args, "confirm=apply", "") } else { "--check --diff " + args } }} 2>&1 | cat

# what secrets each device holds and whether each is the one it should be (its escrowed
# identity key, the keys and passwords its layout declares); reads and compares, shows no secret
[group("backups and secrets")]
[doc("what secrets each device holds and whether each is the one it should be (escrowed, or declared); shows none")]
secrets layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/secrets.yml {{args}} 2>&1 | cat

# escrow each node's identity key into the site's encrypted secrets (site/identity/<board>.sops.yaml);
# writes nothing to a board. -e rekey=true replaces an escrowed key with a board's new one
[group("backups and secrets")]
[doc("escrow the secrets each device made (a node's identity key) into the site's encrypted store; writes nothing to a board (`-e rekey=true` replaces an escrowed key)")]
keys-export layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/keys-export.yml {{args}} 2>&1 | cat

# give each board its escrowed identity key back (an erased or reflashed board becomes the node it
# was); a board that holds its key is left alone. Says who would change; confirm=apply writes
[group("backups and secrets")]
[doc("give each board its escrowed secrets back (an erased or reflashed board becomes the node it was); a plan unless `confirm=apply`")]
keys-restore layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/keys-restore.yml {{ if args =~ '(^| )confirm=apply( |$)' { replace(args, "confirm=apply", "") } else { "--check --diff " + args } }} 2>&1 | cat

# erase one named config region of one board, where its declared image's map (or its chip's
# declared ranges) puts it: an ESP32 (esptool, esp-image), an RP2040/RP2350 (picoboot), a chip
# behind a probe (swd), a plugin's region_erase. With no region named nothing is erased and the
# board's config regions are listed. -e wedged=true for a board that does not answer. The whole
# chip is erase-chip. A plan unless confirm=apply
[group("backups and secrets")]
[doc("erase one named config region of a board (a wedged node's way back); with none named, list them; a plan unless `confirm=apply`")]
erase layout board region="" *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/erase.yml --limit {{board}} -e erase_region={{ if region == "confirm=apply" { "" } else { region } }} {{ if region + " " + args =~ '(^| )confirm=apply( |$)' { replace(args, "confirm=apply", "") } else { "--check --diff " + args } }} 2>&1 | cat

# erase one board's whole flash chip (bootloader, tables, apps, config) after reading that it is
# the registry's chip; refused without a chip backup of it (just backup first) unless
# -e erase_without_backup=true; it must read erased after. A plan unless confirm=apply
[group("backups and secrets")]
[doc("erase a board's whole chip, checked to be the registry's; refused without a backup of it (`-e erase_without_backup=true`); a plan unless `confirm=apply`")]
erase-chip layout board *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/erase-chip.yml --limit {{board}} {{ if args =~ '(^| )confirm=apply( |$)' { replace(args, "confirm=apply", "") } else { "--check --diff " + args } }} 2>&1 | cat

# write one ESP32, RP2040/RP2350 or probe-wired board's whole chip back from a chip backup (default: its newest), whatever ran
# on it since; refused unless the backup was read from this very chip. A plan unless confirm=apply
[group("backups and secrets")]
[doc("write a whole-chip backup back, whatever ran on the board since; refused unless the backup was read from this chip; a plan unless `confirm=apply`")]
restore-chip layout board backup="latest" *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/restore-chip.yml --limit {{board}} -e restore_from={{ if backup == "confirm=apply" { "latest" } else { backup } }} {{ if backup + " " + args =~ '(^| )confirm=apply( |$)' { replace(args, "confirm=apply", "") } else { "--check --diff " + args } }} 2>&1 | cat

# check everything for writing a device's SD card through a registered reader; writes nothing
# (to write: add  -e card_confirm=<reader>  ; that erases the card)
[group("backups and secrets")]
[doc("the checks for writing a device's SD card through a registered reader; `-e card_confirm=<reader>` writes, which erases the card")]
card layout device reader *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/card.yml --limit {{device}} -e card_reader={{reader}} {{args}} 2>&1 | cat

# pull diagnostics from every device of a layout into runs/<time>/<device>/; changes nothing
[group("plan and apply")]
[doc("pull diagnostics from every device of a layout into `runs/<time>/<device>/`; changes nothing")]
diag layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/diag.yml {{args}} 2>&1 | cat

# run a layout's device tests (bench_tests) and keep the results in runs/<time>/<device>/
[group("plan and apply")]
[doc("run a layout's device tests (`bench_tests`); results in `runs/<time>/<device>/`")]
device-test layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/test.yml -e "bench_run=$(date -u +%Y%m%dT%H%M%SZ)" {{args}} 2>&1 | cat

# run a layout's scenarios: steps across devices, judged in order (bench_scenarios, and the library
# tests it binds: bench_test_bindings, <site>/tests/); runs/<time>/scenario/
[group("plan and apply")]
[doc("run a layout's scenarios (`bench_scenarios`, and the library tests it binds, `bench_test_bindings`): steps across devices, each judged on the board it names, `within` seconds to come true; `runs/<time>/scenario/`")]
scenario layout *args:
    l="$({{lay}} {{layout}})" && {{ap}} -i "$l" {{pb}}/scenario.yml -e "bench_run=$(date -u +%Y%m%dT%H%M%SZ)" {{args}} 2>&1 | cat

# the whole test of a bench: apply the layout, run its device tests, then its scenarios
[group("plan and apply")]
[doc("the whole test of a bench: apply the layout, run its device tests, then its scenarios")]
bench-test layout *args:
    just apply {{layout}} {{args}} && just device-test {{layout}} {{args}} && just scenario {{layout}} {{args}}

# a run of a bench's tests that records its state: a plan whose callback writes
# runs/<time>/state.json, then the device tests and scenarios under the same run (docs/ARCHITECTURE.md#runs-tests-and-campaigns)
[group("plan and apply")]
[doc("a run of a bench's tests that records its state: the plan writes `runs/<time>/state.json` (what each board was declared and reported, whether the bench was clean), then its device tests and scenarios under the same run")]
run layout *args:
    ./venv/bin/python tools/run.py {{layout}} {{args}}

# exercise one board: every verb that applies to it, reads first, then writes each undone within the
# run, then (--destructive) the destructive ones each with its restore; the board planned at the
# start and the end; a record per board under runs/<time>/exercise/ (--list says what applies)
[group("plan and apply")]
[doc("exercise one board of a layout: every verb that applies to it, reads first, then writes each undone within the run, then with `--destructive` the destructive ones each with its restore; `--list` says what applies and why, `--skip <verb,..>` leaves verbs out, `--record` keeps the record as the site's latest for the board; the record is `runs/<time>/exercise/<board>.json`. It writes to the board: what would run unless `confirm=apply`")]
exercise layout board *args:
    ./venv/bin/python tools/exercise.py {{layout}} {{board}} {{args}}

# the capability matrix's table from the exercise records the site keeps (<site>/exercise/*.json,
# the newest per board): written between two markers in <site>/docs/CAPABILITY-MATRIX.md, the
# hand-written notes around it kept; --check says whether the file is what the records say
[group("plan and apply")]
[doc("the capability matrix's table from the site's exercise records (`<site>/exercise/*.json`, the newest per board), written between the markers in `<site>/docs/CAPABILITY-MATRIX.md`; `--check` exits 1 when the file is stale, `--stdout` prints the table")]
matrix *args:
    ./venv/bin/python tools/matrix.py {{args}}

# a campaign: a declared sequence of bench states (<site>/campaigns/<name>.yml), each applied,
# checked clean and tested in turn; a failed apply stops it (docs/ARCHITECTURE.md#runs-tests-and-campaigns)
[group("plan and apply")]
[doc("a campaign (`<site>/campaigns/<name>.yml`): each state (a layout, with overrides) planned, applied, checked clean and tested in turn; a failed apply stops it; `runs/<time>/campaign/<name>/`")]
campaign name *args:
    ./venv/bin/python tools/campaign.py {{name}} {{args}}

# what changed between two runs, or between the states of a campaign run: per device test and
# scenario same, changed, only in one or not comparable, beside what changed on the bench
[group("plan and apply")]
[doc("what changed between two runs (`just compare <run> <run>`), or between the states of a campaign run (`just compare <run>`): each device test and scenario, beside what changed on the bench; refuses a run that was not clean unless `--unclean`, and a board read at another identity depth in each run unless `--mixed-depth`; `--json`")]
compare *args:
    ./venv/bin/python tools/compare.py {{args}}

# where the time goes: the timing records of the last runs (or the runs named), slowest first
[group("plan and apply")]
[doc("where the time goes: tasks and module phases from the runs' `timing-<play>.json`, slowest first, with how each moved between the oldest and newest run (`--last N`, or run names)")]
timing *args:
    ./venv/bin/python tools/timing.py {{args}}

# ---- the bench on a PC (docs/SIMULATED.md): Zephyr applications on native_sim, no hardware ----

# build a Zephyr application for native_sim and stage it as a `bare` image of the simulated
# bench (RXRBENCH_ZEPHYR_WORKSPACE, or --workspace W: a west workspace holding a Zephyr).
# `just sim-zephyr-build` makes the two images layout zsim uses.
[group("simulated benches")]
[doc("the Zephyr bench's images for native_sim from any west workspace (`RXRBENCH_ZEPHYR_WORKSPACE`): two shell images (`bare`), two signed SMP server images (`zephyr_mcumgr`)")]
sim-zephyr-build *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{args}}" ]; then ./venv/bin/python tools/zephyr_sim.py build {{args}}; exit; fi
    sim=collections/ansible_collections/rxrbench/core/sim/zephyr
    ./venv/bin/python tools/zephyr_sim.py build --name zephyr-shell --app zephyr/samples/subsys/shell/shell_module
    ./venv/bin/python tools/zephyr_sim.py build --name zephyr-shell-b --app zephyr/samples/subsys/shell/shell_module --conf $sim/prompt-b.conf
    ./venv/bin/python tools/zephyr_sim.py build --name zsmp --app zephyr/samples/subsys/mgmt/mcumgr/smp_svr --conf $sim/smp-shell.conf --overlay $sim/big-flash.overlay --family zephyr_mcumgr --sign 1.0.0
    ./venv/bin/python tools/zephyr_sim.py build --name zsmp-b --app zephyr/samples/subsys/mgmt/mcumgr/smp_svr --conf $sim/smp-shell.conf --conf $sim/zsmp-b.conf --overlay $sim/big-flash.overlay --family zephyr_mcumgr --sign 1.1.0

# build the emulated MCU's images (board zq1: the SMP server under MCUboot on the MPS2 AN385,
# QEMU) at two versions. Needs the Zephyr SDK's Arm toolchain (ZEPHYR_SDK_INSTALL_DIR, or its
# bin on PATH) in the same workspace, and qemu-system-arm to run them.
[group("simulated benches")]
[doc("the emulated MCU's images (`zq`, `zq-b`): Zephyr's SMP server under MCUboot on Arm's MPS2 AN385, run by `qemu-system-arm` (needs the Zephyr SDK)")]
sim-qemu-build *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{args}}" ]; then ./venv/bin/python tools/zephyr_qemu.py build {{args}}; exit; fi
    sim=collections/ansible_collections/rxrbench/core/sim/zephyr
    app=zephyr/samples/subsys/mgmt/mcumgr/smp_svr
    ./venv/bin/python tools/zephyr_qemu.py build --name zq --app $app --conf $sim/smp-shell-mcu.conf --version 1.0.0
    ./venv/bin/python tools/zephyr_qemu.py build --name zq-b --app $app --conf $sim/smp-shell-mcu.conf --conf $sim/zq-b.conf --version 2.0.0

# the simulated bench host: a pinned cloud image in QEMU, reached over ssh on a loopback port
# (registry host vmhost1). setup fetches the image and makes the keys; up serves until stopped
[group("simulated benches")]
[doc("the simulated bench host (registry host `vmhost1`): fetch its pinned cloud image and make its keys")]
sim-host-setup *args:
    ./venv/bin/python collections/ansible_collections/rxrbench/core/sim/vmhost.py setup {{args}}
[group("simulated benches")]
[doc("run the simulated bench host in QEMU, ssh on a loopback port, until stopped")]
sim-host-up *args:
    ./venv/bin/python collections/ansible_collections/rxrbench/core/sim/vmhost.py up {{args}}
[group("simulated benches")]
[doc("stop the simulated bench host")]
sim-host-down *args:
    ./venv/bin/python collections/ansible_collections/rxrbench/core/sim/vmhost.py down {{args}}
[group("simulated benches")]
[doc("an ssh session on the simulated bench host")]
sim-host-ssh *args:
    ./venv/bin/python collections/ansible_collections/rxrbench/core/sim/vmhost.py ssh {{args}}

# the whole bench on a PC: the simulated host up (in the background), the default install and
# the sim bench in it, the simulated boards built; then its tests; then down. The PC bench is
# the examples' (their hosts list names the simulated host; a site's own would not), so these
# run against site.example whatever the site is.
[group("simulated benches")]
[doc("the whole bench on a PC: the simulated host up in the background, with the default install and the simulated boards in it (always against `site.example`)")]
pcbench-up:
    ./venv/bin/python collections/ansible_collections/rxrbench/core/sim/vmhost.py up --daemon
    RXRBENCH_SITE=site.example just hosts-install vmhost1
    RXRBENCH_SITE=site.example just hosts-setup zsim-vm
[group("simulated benches")]
[doc("plan, apply, device tests and scenarios of layout `zsim-vm` on the PC bench")]
pcbench-test *args:
    RXRBENCH_SITE=site.example just plan zsim-vm {{args}}
    RXRBENCH_SITE=site.example just bench-test zsim-vm {{args}}
[group("simulated benches")]
[doc("stop the PC bench")]
pcbench-down:
    ./venv/bin/python collections/ansible_collections/rxrbench/core/sim/vmhost.py down

# the simulated Zephyr bench: the boards of core's sim registry behind real brokers, until
# stopped (layout zsim; RXRBENCH_SIM_RUN is where the sockets, logs and flash files go).
# The seventh board, zq1, is the emulated MCU (just sim-qemu-build, qemu-system-arm).
[group("simulated benches")]
[doc("run the simulated Zephyr bench (layout `zsim`) behind real brokers, until stopped; `zq1` is the emulated MCU")]
sim-zephyr-up *boards='zb1 zb2 zb3 zb4 zb5 nb1 zq1':
    ./venv/bin/python collections/ansible_collections/rxrbench/core/sim/simbench.py --registry site.example/sim/registry.yaml --run-dir "${RXRBENCH_SIM_RUN:-/tmp/rxrbench-sim}" {{boards}}

# The collections' modules, plugins and roles (antsibull-docs, Sphinx) and the Python API
# (pdoc), from the pinned doc tools (docs/tools/requirements.txt, their own venv).
# the generated reference into .build/docs (docs/tools)
[group("the tool")]
[doc("the generated reference into `.build/docs`: the collections' modules, plugins and roles (antsibull-docs, Sphinx) and the Python API (pdoc), from the pinned doc tools")]
docs:
    ./venv/bin/python tools/docs.py build

# antsibull-docs' lint of every collection's docs (what CI runs)
[group("the tool")]
[doc("antsibull-docs' lint of every collection's docs, as CI runs it")]
docs-lint:
    ./venv/bin/python tools/docs.py lint

# docs/LAYOUT.md's tables, and each role spec's entry of a layout variable, from docs/layout-vars.yml
[group("the tool")]
[doc("write `docs/LAYOUT.md`'s tables and the role specs' layout variables from `docs/layout-vars.yml` (a unit test checks them)")]
docs-layout:
    ./venv/bin/python tools/docs.py layout

# docs/COMMANDS.md's and the plugin READMEs' commands tables, from the recipes' [group] and [doc]
[group("the tool")]
[doc("write the commands tables (`docs/COMMANDS.md`, each plugin README) from the recipes' `[group]` and `[doc]` (a unit test checks them)")]
docs-readme:
    ./venv/bin/python tools/docs.py commands

# serve .build/docs on 127.0.0.1 (building it first when it is not there)
[group("the tool")]
[doc("serve `.build/docs` on 127.0.0.1, building it first when it is not there")]
docs-serve port='8765':
    ./venv/bin/python tools/docs.py serve {{port}}

# unit tests, the sim tests (each skipped until its images are built: sim-zephyr-build, sim-qemu-build,
# sim-nuttx-build, sim-zapp-build, mesh's sim-build) and lint; needs no hardware
[group("the tool")]
[doc("unit and sim tests (each sim skipped until its images are built), lint, the privacy check; needs no hardware")]
test:
    ./venv/bin/pytest -q tests/unit tests/sim
    ./venv/bin/yamllint -s .
    ./venv/bin/ansible-lint 2>&1 | cat
    ./venv/bin/python tools/privacy_check.py
