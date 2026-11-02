"""The failure text every tree check gives (docs/ARCHITECTURE.md#the-tools-own-tests):
the requirement it protects and the ways out of a failure.

A check is a proxy for a requirement written down elsewhere. Its failure names that
requirement and the legitimate ways out, so a reader decides from the message alone whether
to fix the design, record a reasoned exception, or ask, and never makes the check pass by
changing only what it sees.
"""

DO_NOT = "Unsure: ask. Do not reword, rename or reroute code only to make this check pass."


def breach(found, protects, fix, exception=None):
    """The message for `found` (lines, one per hit): what matched, the requirement
    (`protects`, with the doc that states it), how to fix a real breach, and the exception form
    for a false alarm (None: the check has none, so a false alarm is a fault in the check)."""
    lines = list(found)
    out = ["\n".join(lines) if lines else "(nothing matched)",
           f"Protects: {protects}",
           f"If the requirement is broken: {fix}"]
    if exception:
        out.append(f"If it holds and this is a false alarm: {exception}")
    else:
        out.append("If it holds and this is a false alarm: the check is wrong; fix the check "
                   "(docs/ARCHITECTURE.md#the-tools-own-tests), with the case as a test.")
    out.append(DO_NOT)
    return "\n".join(out)
