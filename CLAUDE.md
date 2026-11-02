# CLAUDE.md — rxrbench

Declarative bench automation for embedded devices, on Ansible: a layout declares devices and
their state; `--check --diff` is the plan, a run the apply.

Read [CONTRIBUTING.md](CONTRIBUTING.md) (setup, tests, the tree's rules) and
[docs/OVERVIEW.md](docs/OVERVIEW.md) (the tool in one read) before changing anything. If a
`CLAUDE.local.md` exists beside this file, it describes the site in use: read it too.

## Rules an agent must not break

- **`devreg guard` before every write to hardware.** A device is resolved by name, never by a
  raw path, and never by a guess between two matches. A BLE or ssh route is checked the same
  way (`devreg guard --route`).
- **Plan before apply.** Run the plan (`just plan`, or a confirm-gated recipe without
  `confirm=apply`) and read it before any write. Apply is opt-in per layout
  (`bench_apply_allowed`), a first flash per board (`bench_first_flash`). `boot`,
  `restore-regions`, `restore-chip`, `switch`, `keys-restore`, `erase` and `erase-chip` are
  plans unless `confirm=apply` is among their arguments.
- **Success is what the device says afterwards**, never what a tool printed: read, change only
  what differs, read again.
- **No site data in the tree.** Layouts, registry, facts, hostnames, users, addresses, chip ids,
  MACs and secrets live in the site (`site/`, gitignored) or `.env`
  ([CONTRIBUTING.md, Public and private](CONTRIBUTING.md#public-and-private)). Secrets never
  reach a log, a remote argv or a commit.
- **Hooks are never bypassed.** No `--no-verify`. A failing check protects a requirement: fix
  what is broken, or use the check's exception form. Never reword or route around a scanner
  to make it pass. If unsure, stop and ask.
- **Tests before commit.** `just test` passes; a change to the inventory plugin, the layers,
  the families or the roles also leaves `just golden check` clean.
- **Core knows no plugin.** Firmware-, board- or update-system-specific knowledge goes in a
  plugin or catalog data ([docs/PLUGINS.md](docs/PLUGINS.md)).
- Ansible refuses a non-blocking stderr: recipes pipe it through `cat`, under `pipefail`.
- Conventional commits, no AI attribution.
