"""Which depth a read takes: what was asked, unless the moment forces a full read
(docs/ARCHITECTURE.md#checking-depth).

    checked = depth.resolve(asked, read="before write", spot_reads=flasher.spot_reads,
                            first_flash=first, family_switch=switch)
    # {"depth": "full" | "spot", "reason": "..."}

A read takes `spot` only when it was asked, no moment forces full, and the read has a spot form
(the reader lists it in `spot_reads`). Every result carries the outcome as `checked`.
"""
DEPTHS = ("full", "spot")

#: The moments that read in full whatever is asked, and how a result says so.
FORCED = {
    "first_flash": "a first flash reads in full",
    "family_switch": "a family switch reads in full",
    "bootloader": "a bootloader change reads in full",
    "table_change": "a write that changes the partition table reads in full",
    "erase": "an erase reads in full",
    "restore": "a restore reads in full",
    "no_full_record": "no full record yet for the firmware the board reports",
    "apply": "an apply asks the board what it runs: that read decides the write",
}


def resolve(asked=None, read=None, spot_reads=(), **moments):
    unknown = set(moments) - set(FORCED)
    if unknown:
        raise ValueError(f"unknown moments: {', '.join(sorted(unknown))}")
    asked = asked or "full"
    if asked not in DEPTHS:
        raise ValueError(f"check depth is {asked!r}: one of {', '.join(DEPTHS)}")
    if asked == "full":
        return {"depth": "full", "reason": "asked"}
    for moment, why in FORCED.items():
        if moments.get(moment):
            return {"depth": "full", "reason": why}
    if read not in spot_reads:
        return {"depth": "full", "reason": f"{read or 'this read'} has no spot form"}
    return {"depth": "spot", "reason": "asked"}
