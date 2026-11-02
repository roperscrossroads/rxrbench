"""The state line: one machine-readable line a firmware prints about itself (rxrbench's
state-line protocol, docs/PLUGINS.md; first printed by the mesh plugin's own firmware).

    ~S{json}*crc16        crc_hqx of the JSON text, four hex digits

A firmware that prints one can be read without knowing its console's prose. What the JSON
holds is the firmware's own; a family says which of its keys name the image (familyspec.py).
"""
import binascii
import json
import re

STATE_LINE = re.compile(r"~S(\{.*\})\*([0-9a-f]{4})\s*$", re.M)


class StateError(ValueError):
    pass


def parse_line(output, unknown=()):
    """The JSON of a state line, checked; None when the output has no state line at all. A
    line that is there but damaged raises: it must not be read as "no state". `unknown` are
    placeholders a firmware prints where it cannot format a value, read as null."""
    m = STATE_LINE.search(output)
    if m is None:
        if "~S" in output:
            raise StateError("the state line is incomplete")
        return None
    text, crc = m.group(1), int(m.group(2), 16)
    if binascii.crc_hqx(text.encode(), 0) != crc:
        raise StateError("the state line failed its checksum")
    for token in unknown:      # after the checksum, which is over the line as printed
        text = text.replace(token, "null")
    try:
        return json.loads(text)
    except ValueError as e:
        raise StateError(f"the state line is not JSON: {e}") from e


def read(board, spec, deadline=10.0):
    """The JSON of a board's state line through its broker, asked as `spec` says (a family's
    `identity`: command, dialect, null_tokens); None when the answer holds no state line."""
    kw = {"dialect": spec["dialect"]} if spec.get("dialect") else {}
    return parse_line(board.exec(spec["command"], deadline=deadline, **kw),
                      unknown=spec.get("null_tokens") or ())
