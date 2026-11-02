# devreg: named hardware, and the gate in front of every write

Resolve a device by **name**, never by `/dev/sdX`, a `/dev/ttyUSB` number or a USB bus id: those
follow plug order, and two boards of one model look the same by VID:PID. Every rxrbench write to
hardware goes through devreg: `resolve <name>` first, `guard <name> <path>` right before the
write. Identity comes from the site's registry, presence from the host's sysfs.

The registry's keys, where it is read from, why ambiguity is an error and how `just enroll`
writes entries: [`docs/REGISTRY.md`](../docs/REGISTRY.md).

## Usage

```bash
devreg list [--host H]            # registered devices and whether each is present now
devreg scan [--host H] [--hubs]   # present hardware NOT yet registered, as a paste-ready block
devreg resolve <name>             # the path; rc 1 absent, rc 2 ambiguous, never a guess
devreg resolve --route <bearer> <name>   # the address the registry records for that route
devreg resolve --busid <name>     # its place on the USB bus (1-1.2.4), what openocd is handed
devreg guard <name> <path>        # assert <path> IS <name>; refuses protected and mounted paths
devreg guard --route <bearer> <name> <address>
devreg whois <path>               # which registered device is at this path
devreg where [<name>]             # which host has it (asks every host)
devreg reach <name> [-v]          # every recorded route and what each can do
devreg hosts                      # the hosts the registry knows, and which answer
devreg policy <name> [<key>]      # a device's policy block as key=value lines
devreg busy [<name>]              # is its port held, by whom (rc 1 = busy); `holders` lists all
devreg deploy [<host>...]         # push a self-contained copy (script + registry) to bench hosts
devreg lint [--strict]            # keys beyond the contract, and shapes it refuses
```

From an rxrbench checkout, `just devreg <args>` runs it against the site's registry.

| rc | meaning |
|---|---|
| 0 | ok |
| 1 | registered but not present (for `where`, nowhere) |
| 2 | **ambiguous**: more than one device matched |
| 3 | refused: a protected path, a mounted filesystem, or a guard mismatch |
| 4 | registry or usage error |

## Before any destructive write

```bash
dev="$(devreg resolve board1)" || exit 1
devreg guard board1 "$dev" || exit 1
# only now: dd, esptool, flashrom, a card writer
```

## Limits

- `guard` checks the local host only; a bench host writes with its own deployed copy.
- `reach` never probes: it reports what the registry records.

## For an agent driving the bench

[`AGENTS.md`](AGENTS.md) holds the rules an automated agent or script must keep before it
touches hardware. Copy it into the instructions that agent reads on every machine where it can
reach boards.
