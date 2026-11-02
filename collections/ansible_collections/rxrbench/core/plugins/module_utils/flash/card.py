"""Writing a whole disk image to a removable card: the one flash that can destroy the
machine it runs on.

The write itself is a site's own kit (RXRBENCH_CARD_KIT, the path of its `write-card.sh`):
it refuses a partition, a disk the running system uses, anything mounted, anything not
removable, writes with bmaptool and reads the written ranges back. This module adds what
the tool can know and the kit cannot:

- the target is a device the REGISTRY lists as a block device meant to be written
  (`kind: block`, `destructive: true`), named, never a raw path from a variable;
- `devreg` resolves it and `devreg guard` vouches for the path (protected disks, mounts,
  the name matching the device);
- the image is what the layout declares for the device the card is for (its `compatible`);
- the caller said the device's name again, on purpose (`confirm`).

A card has no running firmware to ask, so there is no "already written": every run writes.
The kit needs root. This runs it with `sudo -n`: without passwordless sudo it stops at the
first privileged step having written nothing, and says to run the write attended.
"""
import os

from . import common

FORMATS = ("wic",)


class CardWriter:
    def __init__(self, device, entry, manifest, image_file, kit, confirm=None,
                 compatible=None, devreg="devreg", runner=common.tool, planning=False):
        self.device, self.entry = device, entry or {}
        self.manifest, self.image = manifest, image_file
        self.kit = os.path.expanduser(kit) if kit else None
        self.confirm, self.compatible = confirm, compatible
        self.devreg, self.run = devreg, runner
        # A plan makes every check but the confirmation: it writes nothing to confirm.
        self.planning = planning
        self.path = None

    def preflight(self):
        """Every reason not to write. Empty when the write may go ahead; self.path is then
        the guarded device path."""
        out = common.common_refusals(self.manifest, FORMATS)
        if not self.kit:
            out.append("no card-writing kit: set RXRBENCH_CARD_KIT (its write-card.sh)")
        if self.entry.get("kind") != "block":
            out.append(f"{self.device} is not a block device in the registry "
                       f"(kind: {self.entry.get('kind')!r}): only a registered card reader "
                       f"or card is written")
        if not self.entry.get("destructive"):
            out.append(f"the registry does not mark {self.device} as meant to be overwritten "
                       f"(destructive: true)")
        if not self.planning and self.confirm != self.device:
            out.append(f"not confirmed: say the device's name again (confirm={self.device})")
        if self.compatible is not None and self.manifest.get("target") != self.compatible:
            out.append(f"the image is {self.manifest.get('target')!r}; the card is for a "
                       f"device that is {self.compatible!r}")
        if not os.path.isfile(self.image + ".bmap"):
            out.append("no .bmap beside the image: the kit would fall back to dd, with no "
                       "per-block checksums")
        if self.kit and not os.access(self.kit, os.X_OK):
            out.append(f"no card-writing kit at {self.kit}")
        if out:
            return out          # do not go looking for hardware for a write already refused
        rc, text = self.run([self.devreg, "resolve", self.device], 30)
        path = text.strip().splitlines()[-1] if text.strip() else ""
        if rc != 0 or not path.startswith("/dev/"):
            return [f"devreg cannot resolve {self.device}: {text.strip()[-200:]}"]
        rc, text = self.run([self.devreg, "guard", self.device, path], 30)
        if rc != 0:
            return [f"devreg guard refuses {path} for {self.device}: {text.strip()[-200:]}"]
        self.path = path
        return []

    def write(self):
        """Run the kit on the guarded path. Its own guards run again; its typed confirmation
        is replaced by `confirm`, which preflight has checked."""
        rc, text = self.run(["sudo", "-n", "true"], 15)
        if rc != 0:
            raise common.FlashError(
                "writing a card needs root and this account's sudo asks for a password: "
                "nothing was written. Run the write attended (`just card ...` prints the "
                "command), or allow this one command without a password.")
        rc, text = self.run([self.kit, self.path, self.image], 3600, env={"YES": "1"})
        if rc != 0 or "VERIFIED:" not in text:
            raise common.FlashError(f"the card kit failed (rc {rc}); do NOT boot this card: "
                                    + text.strip()[-400:])
        return text
