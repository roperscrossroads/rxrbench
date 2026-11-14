# Contributing to rxrbench

rxrbench is alpha software; bug reports, hardware results and plugins are welcome. [docs/OVERVIEW.md](docs/OVERVIEW.md) explains the tool itself;
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) how it is built; [docs/PLUGINS.md](docs/PLUGINS.md)
how to add a device family.

## Setup

```bash
git clone https://github.com/roperscrossroads/rxrbench && cd rxrbench
just setup        # the venv, the collections, sops and age (pinned), the git hooks
just doctor       # what this machine has of what the simulated benches need
```

`just setup` sets `core.hooksPath` to `hooks/`, so the hooks below run on every commit and push.
[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) has the full requirements.

## Running the tests

| Command | What it runs |
|---|---|
| `just test` | the unit and sim tests, yamllint, ansible-lint (production profile) and the privacy check; needs no hardware |
| `./venv/bin/pytest -q -n auto --dist loadfile tests/unit` | the unit tests alone, one worker per core (pytest-xdist; each file stays in one worker): a minute or two |
| `./venv/bin/pytest -q tests/sim` | the simulated benches; each is skipped until its images are built ([docs/SIMULATED.md](docs/SIMULATED.md)) |
| `just core-only` | the unit tests against a collections tree holding only `rxrbench.core` and a fixture plugin: core must work without any bundled plugin |
| `just golden check` | every layout resolved and compared with its recorded form |
| `just privacy` | the privacy check alone |
| `just ci [job]` | one GitHub Actions job, run locally with `act` on a clean clone of HEAD |

The hardware tests in `tests/hw/` run only when named, never from `just test`
([tests/hw/README.md](tests/hw/README.md)).

`just test` passes before every commit. A change to the inventory plugin, the layers, the
families or the roles also leaves `just golden check` clean; when the change to a resolved
layout is intended, re-record (`just golden record --example`) and say in the commit which
keys changed and why.

Generated text is never edited by hand. `just docs-readme` writes the recipe tables
(`docs/COMMANDS.md` and each plugin README, from each recipe's `[group]` and `[doc]`);
`just docs-layout` writes the variable tables in `docs/LAYOUT.md` from `docs/layout-vars.yml`.
Unit tests fail when either is stale.

### The git hooks

| Hook | What it checks |
|---|---|
| `pre-commit` | the staged files, by `tools/privacy_check.py --staged` |
| `pre-merge-commit` | the same, for a merge |
| `commit-msg` | the commit message, by `tools/privacy_check.py --msg` |
| `pre-push` | the whole tracked tree, and the message of every commit about to be pushed (an internal remote: the tree only, see below) |

A hook is never bypassed (`--no-verify`). When one fails, fix what it found.

## The tree's rules

### Voice

`tests/unit/test_hygiene.py` checks every tracked text file. Comments and docs:

- are in the third person, and say what holds and why;
- are undated: history belongs in `CHANGELOG.md`, and a dated hardware observation says
  `observed` on its line;
- name nothing a reader of the public tree cannot open (no private tracker ids, no private
  documents).

### Checks protect requirements

Every check (a test that scans the tree, a ratchet, a lint rule, a hook) protects a
requirement its failure message names. When a check fails, ask whether the requirement
is broken. If it is, fix the code. If it is not, use the exception form the check names, or fix
the check. Rewording, renaming, moving a fact into prose or routing code around a scanner only
to make a check pass is never the fix.

### Core and the plugins

`rxrbench.core` holds no knowledge of one firmware project, board product or update system,
never chooses by a family's or plugin's name, and imports no plugin. That knowledge lives in a
plugin or in catalog data ([docs/PLUGINS.md](docs/PLUGINS.md)). `just core-only` and
`tests/unit/test_families.py` guard it.

### The registry contract

The keys the tool reads from a device registry are listed in [docs/REGISTRY.md](docs/REGISTRY.md),
and `tests/unit/test_registry_contract.py` holds the code to that list: every string key a
registry reader subscripts is recorded in `tests/fixtures/registry-keys.txt`. A new key read
fails the test. To add one, name it in `docs/REGISTRY.md`, then re-record:

```bash
RATCHET_RECORD=1 ./venv/bin/pytest -q tests/unit/test_registry_contract.py
```

Re-recording is legitimate only for a new key `docs/REGISTRY.md` now documents, or for a key
that is not a registry key (a sysfs or route field the reader also subscripts); never to make
a site's own key a requirement.

## Public and private

rxrbench is public. What belongs to one bench lives in a **site**: a directory (`site/` by
default, or `RXRBENCH_SITE`) that is gitignored here and is usually its own private repository.
`site.example/` shows its shape.

| Public: this repository | Private: the site, or `.env` |
|---|---|
| code, roles, playbooks, tests, the plugin collections, the generic catalog | layouts, the device registry and survey facts, a site catalog, a site's own plugins |
| `site.example/`, `.env.example`, `.sops.yaml.example` | `.env`, `<site>/.sops.yaml`, every `*.sops.yaml` |
| the docs in `docs/` | the bench's own history, plans and decisions |
| `privacy-patterns.txt`: generic private patterns | `<site>/private-patterns.txt`: the site's hostnames, users, domains, paths |

Never committed here: hostnames, users, addresses, real chip ids, MACs or node ids, people,
secrets, or a site path in code. A site's values reach code only through `.env` and
`module_utils/site.py`. Code, tests and examples use hosts `bench1`, `benchpi1` or `bench-a`,
user `bench`, the documentation address ranges (192.0.2.0/24, 2001:db8::/32), domain
`example.org`, and paths under `~` or relative.

`tools/privacy_check.py` enforces this through `just privacy`, `just test` and the hooks. It
reads two pattern lists, one regular expression per line: the generic `privacy-patterns.txt`
(private address ranges, internal DNS suffixes, home directories, email addresses) and the
site's own. The site's chip ids, MACs and node ids from its registry and facts are added
without a list. Any other token shaped like a chip id (16 hex digits) or a MAC must be plainly
made up (`0123456789ABCDEF`, `02:00:00:00:00:10`) or listed, after checking, in
`tests/fixtures/made-up-ids.txt`. A line that must keep an example that looks private may carry
`privacy: allow`; never a real value.

**Internal remotes.** A private forge on the site's own network may hold the history with the
site's names in its messages. Mark it so the pre-push hook checks only the tree:

```bash
git config --bool remote.<name>.internal true
```

A public remote gets a history whose every message passes `tools/privacy_check.py --msg`, or a
fresh history started from a clean tree.

## Proposing a change

- **A bug fix** that restores documented behaviour: a pull request with a test that fails
  without it.
- **A larger change** (a new capability; a changed layout, registry key, family hook or module
  option; a new or changed check): open an issue first. Describe the problem, with evidence (a
  failing run, a board's output, a question the docs cannot answer), and the intended
  behaviour, as properties that can be checked. Implementation starts once the approach is
  agreed.
- **Hardware results**: an issue naming the board, the firmware, the command and what it
  printed.

Commits are [conventional](https://www.conventionalcommits.org/) (`feat(mesh): ...`,
`fix(core): ...`, `docs: ...`), one logical change each, and update the doc that owns what they
change.
