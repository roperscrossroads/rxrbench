# Rules for an agent driving the bench

Copy this into the instructions any automated agent or script reads on a machine with boards
attached.

Raw device paths are not stable (`/dev/sdX` follows plug order; two boards of one model look the
same by VID:PID), and several sessions may share the hardware. **Resolve by name through
`devreg`; never hardcode a device path in a command that writes.**

- **`devreg guard <name> <path>` immediately before every destructive write** (`dd`, `esptool`,
  `flashrom`, a card writer), and stop on a non-zero exit.
- **Ambiguity is an error, not a coin-flip.** When `devreg resolve` returns rc 2, add a serial to
  the registry or unplug the other board; never pick one.
- **Register new hardware before working on it**: `devreg scan` with the board plugged in alone,
  then paste the block. Prefer a serial-bearing match over a bare VID:PID.
- A board that is not in the registry is added, never worked around.

Usage: `devreg/README.md`. Registry keys: `docs/REGISTRY.md`.
