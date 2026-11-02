"""Protobuf enum values the firmware prints as numbers, and their stock names.

Taken from the firmware's generated config.pb.h. tests/unit/test_enums.py compares this table
against that header whenever a firmware build tree is on the machine, so a protobuf update
that adds or moves a value is caught here and not on a board.
"""

MODEM_PRESET = {
    0: "LONG_FAST", 1: "LONG_SLOW", 2: "VERY_LONG_SLOW", 3: "MEDIUM_SLOW", 4: "MEDIUM_FAST",
    5: "SHORT_SLOW", 6: "SHORT_FAST", 7: "LONG_MODERATE", 8: "SHORT_TURBO", 9: "LONG_TURBO",
    10: "LITE_FAST", 11: "LITE_SLOW", 12: "NARROW_FAST", 13: "NARROW_SLOW", 14: "TINY_FAST",
    15: "TINY_SLOW", 16: "MEDIUM_TURBO",
}
REGION = {
    0: "UNSET", 1: "US", 2: "EU_433", 3: "EU_868", 4: "CN", 5: "JP", 6: "ANZ", 7: "KR", 8: "TW",
    9: "RU", 10: "IN", 11: "NZ_865", 12: "TH", 13: "LORA_24", 14: "UA_433", 15: "UA_868",
    16: "MY_433", 17: "MY_919", 18: "SG_923", 19: "PH_433", 20: "PH_868", 21: "PH_915",
    22: "ANZ_433", 23: "KZ_433", 24: "KZ_863", 25: "NP_865", 26: "BR_902", 27: "ITU1_2M",
    28: "ITU2_2M", 29: "EU_866", 30: "EU_874", 31: "EU_917", 32: "EU_N_868", 33: "ITU3_2M",
    34: "ITU1_70CM", 35: "ITU2_70CM", 36: "ITU3_70CM", 37: "ITU2_125CM",
}
DEVICE_ROLE = {
    0: "CLIENT", 1: "CLIENT_MUTE", 2: "ROUTER", 3: "ROUTER_CLIENT", 4: "REPEATER", 5: "TRACKER",
    6: "SENSOR", 7: "TAK", 8: "CLIENT_HIDDEN", 9: "LOST_AND_FOUND", 10: "TAK_TRACKER",
    11: "ROUTER_LATE", 12: "CLIENT_BASE",
}
REBROADCAST_MODE = {
    0: "ALL", 1: "ALL_SKIP_DECODING", 2: "LOCAL_ONLY", 3: "KNOWN_ONLY", 4: "NONE",
    5: "CORE_PORTNUMS_ONLY",
}
CHANNEL_ROLE = {0: "DISABLED", 1: "PRIMARY", 2: "SECONDARY"}


def name(table, value, what):
    """The stock name of an enum value; an unknown value is named as such, never guessed."""
    return table.get(value, f"{what}_{value}")
