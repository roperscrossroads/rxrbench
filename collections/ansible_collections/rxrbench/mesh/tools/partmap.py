#!/usr/bin/env python3
"""Extract the flash layout from a built Zephyr DTS — the whole layout, no guesses.

Everything a flasher needs comes from the devicetree the image was actually
built against, so `flash.json` describes the board in front of you rather than
whichever board someone had in mind when the tooling was written.

This script has no defaults. A layout that is seeded with another board's map and patched
where the parse succeeds describes the wrong board whenever the parse fails, and a flash size
inferred from a partition's offset breaks when the map is reorganised: the flash size is read
from the flash node.

So: missing data is an ERROR with a non-zero exit, never a fallback. A caller
that cannot get a real answer must stop, not carry on with a plausible one.

Usage:
  partmap.py <build>/zephyr/zephyr.dts [options]

  --json                 emit JSON instead of shell KEY=VALUE lines
  --require A,B,...      exit non-zero unless every named key was found
  --set KEY=VALUE        override a key (repeatable) — see below

Emits FLASH_SIZE plus, for every partition in the tree, <NAME> and <NAME>_SIZE
(e.g. slot0_partition -> SLOT0, SLOT0_SIZE). New partitions appear on their own;
nothing here needs teaching about a board.

--set exists for the chicken-and-egg case: rearranging a flash map, where the
DTS you are building is not yet the map on the board you must talk to. It is
deliberately loud — every override is announced on stderr and recorded in the
JSON under "overrides" — because a silent override is the bug this file was
rewritten to remove.
"""
import argparse
import json
import re
import sys

# A DTS `reg = < 0xOFF 0xSIZE >;` possibly followed by a /* provenance */ comment.
_REG = r"reg\s*=\s*<\s*(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s*>"


def flash_size(dts):
    """Size of the SoC flash, from the flash node itself.

    Anchored on compatible = "soc-nv-flash" rather than a node label, because
    the label differs across SoC families and the compatible does not. Takes the
    first reg after that compatible, which is the flash node's own — the
    partitions nested below it have regs too, and matching one of those would
    silently report a partition size as the chip size.
    """
    m = re.search(r'compatible\s*=\s*"soc-nv-flash"\s*;', dts)
    if not m:
        return None
    m2 = re.search(_REG, dts[m.end():])
    return m2.group(2) if m2 else None


def partitions(dts):
    """Every `<name>_partition: partition@<addr> { ... reg = <off size> }`."""
    out = {}
    for m in re.finditer(
        r"(\w+)_partition:\s*partition@[0-9a-fA-Fx]+\s*\{(.*?)\}", dts, re.S
    ):
        name, body = m.group(1).upper(), m.group(2)
        r = re.search(_REG, body)
        if r:
            out[name] = (r.group(1), r.group(2))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dts")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--require", default="")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = ap.parse_args()

    try:
        dts = open(args.dts).read()
    except OSError as exc:
        sys.exit("partmap: cannot read %s: %s" % (args.dts, exc))

    vals = {}
    fs = flash_size(dts)
    if fs:
        vals["FLASH_SIZE"] = fs
    for name, (off, size) in partitions(dts).items():
        vals[name] = off
        vals[name + "_SIZE"] = size

    if not vals:
        sys.exit("partmap: no flash node and no partitions found in %s — this is not a "
                 "built zephyr.dts, or the format changed. REFUSING to guess a layout."
                 % args.dts)

    overrides = {}
    for item in args.set:
        if "=" not in item:
            sys.exit("partmap: --set wants KEY=VALUE, got %r" % item)
        k, v = item.split("=", 1)
        k = k.strip().upper()
        was = vals.get(k)
        vals[k] = v.strip()
        overrides[k] = {"from": was, "to": vals[k]}
        # Loud, on stderr, always. An override that cannot be seen in the log is
        # indistinguishable from the silent fallback this rewrite deleted.
        print("partmap: OVERRIDE %s: %s -> %s"
              % (k, was if was is not None else "(not in DTS)", vals[k]), file=sys.stderr)

    missing = [k for k in (x.strip().upper() for x in args.require.split(",") if x.strip())
               if k not in vals]
    if missing:
        sys.exit("partmap: %s not found in %s (have: %s). REFUSING to substitute a "
                 "default — pass --set %s=0x… if you are mid-rearrangement."
                 % (", ".join(missing), args.dts, " ".join(sorted(vals)), missing[0]))

    if args.json:
        print(json.dumps({"values": vals, "overrides": overrides,
                          "source": args.dts}, indent=2))
    else:
        for k in sorted(vals):
            print("%s=%s" % (k, vals[k]))


if __name__ == "__main__":
    main()
