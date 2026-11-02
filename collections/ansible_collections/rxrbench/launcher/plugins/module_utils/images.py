"""What an image is, for Launcher: the app Launcher installs from it, and how it is known.

Two kinds of image reach a Launcher board:

- a **manager** image: Launcher itself, the merged file a release ships for one board
  environment (bootloader, the shipped partition table at 0x8000, Launcher at 0x10000). It is
  written whole at 0x0 on a chip that has no Launcher yet, and installed as an app (its app
  part alone, which copies itself into Launcher's partition) on one that has;
- an **app** image: a firmware Launcher installs into a slot: a plain ESP-IDF app image, or a
  merged (factory) image whose app part is cut out at install time, as Launcher's own tools do.

An installed app's bytes cannot be read back through Launcher, so an image is known by its
sha256 and size (what the install record keeps), and the app part by its offset and length.
"""
import hashlib

from ansible_collections.rxrbench.core.plugins.module_utils import bootid, esppart


class ImageError(ValueError):
    pass


def app_part(data):
    """(offset, length) of the ESP-IDF app in `data`: the image itself when it starts with
    one; the app partition's contents when it is a merged image with a partition table. The
    length is the image's own (header, segments, checksum, appended hash), never the slot's."""
    rows = esppart.parse(data, esppart.OFFSET) if len(data) > esppart.OFFSET else []
    if rows:
        apps = [r for r in rows if r["type"] == esppart.APP]
        if not apps:
            raise ImageError("a merged image with a partition table but no app partition")
        first = min(apps, key=lambda r: r["offset"])
        n = bootid.esp_image_length(data[first["offset"]:])
        if not n:
            raise ImageError(f"no ESP-IDF app image at {first['offset']:#x} of the merged image")
        return first["offset"], n
    n = bootid.esp_image_length(data)
    if not n:
        raise ImageError("not an ESP-IDF app image, nor a merged image with a table")
    return 0, n


def describe(path):
    """{path, size, sha256, app_offset, app_size, merged, manager}: what the planner and the
    install record need of one file. `manager`: the merged image carries an app manager's own
    table (it is a Launcher release)."""
    with open(path, "rb") as f:
        data = f.read()
    off, n = app_part(data)
    rows = esppart.parse(data, esppart.OFFSET) if len(data) > esppart.OFFSET else []
    return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "app_offset": off, "app_size": n, "merged": bool(rows),
            "manager": esppart.manager(rows) is not None}


def slot_bytes(app_size, align=0x10000):
    """The slot Launcher makes for an app of `app_size` bytes: 64 KB aligned."""
    return -(-app_size // align) * align


#: A slot label is six characters: Launcher makes it of the app's name.
LABEL_LEN = 6


def slot_label(name):
    """The partition label Launcher gives an app installed under `name`, when that label is
    free (`partition_table_model.cpp`): lowercased, letters and digits only, the first six,
    padded with zeros to six; `app000` of a name with none. `buddy` -> `buddy0`, `cam-stream`
    -> `camstr`, `M9-MUI` -> `m9mui0`. A taken label gets its sixth character replaced by 1..9,
    then 0, then `appN`: declare the label that is free, or the one the slot already has."""
    base = "".join(c for c in name.lower() if c.isalnum() and c.isascii())[:LABEL_LEN]
    return (base or "app").ljust(LABEL_LEN, "0")


def next_slot_label(name, taken):
    """`slot_label(name)`, or the label Launcher picks when it is taken (the same code)."""
    base = slot_label(name)
    if base not in taken:
        return base
    for i in list(range(1, 10)) + [0]:
        candidate = base[:5] + str(i)
        if candidate not in taken:
            return candidate
    for i in range(1, 100):
        if f"app{i}" not in taken:
            return f"app{i}"
    return "app"
