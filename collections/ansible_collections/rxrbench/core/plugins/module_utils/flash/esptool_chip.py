"""The esptool route to a whole ESP32 chip: every byte of its flash read through the ROM
loader, written back block by block, or erased (chip.py's chip_blocks sessions). What
chip_backup, chip_restore and chip_erase do for a board esptool writes, on the one chip flow
(chip_flow.py, docs/ARCHITECTURE.md#chip-and-region-flows). An erase needs the chip's MAC from the
registry and is made in the loader session that reads it.

The chip is the one whose factory MAC the loader reads: a backup records it, and a restore goes
back only to that chip, of the same flash size. The board's broker lets go of the port for the
session; a board that said what it ran must come back as it was (after a backup), or as the
backup recorded it (after a restore). One that answered nothing is only given its console back.
A restore of a backup without the app manager the chip carries needs the layout's word
(`bench_app_manager_overwrite`), since it would orphan every installed app.
"""
import contextlib
import os

from .. import broker_client, esppart, identity, stateline
from . import chip, chip_flow, common


def identity_of(b, family):
    if not family:
        return None, "no family given"
    try:
        return common.read_board(b, family), None
    except (common.FlashError, broker_client.BrokerError, stateline.StateError) as e:
        return None, str(e)


def _manager_in(raw):
    with open(raw, "rb") as f:
        f.seek(esppart.OFFSET)
        return esppart.manager(esppart.parse(f.read(esppart.LENGTH)))


class _Session(chip_flow.ChipSession):
    """The broker lets go of the port for the whole session; the loader sessions inside each
    boot the board after (chip.booted_after)."""

    def __enter__(self):
        r, p = self.route, self.route.p
        self.unread, self.expect, self.family = None, None, p.get("family")
        self.stack = contextlib.ExitStack()
        b = self.stack.enter_context(broker_client.board_or_none(
            p["board"], path=p["socket"], timeout=p["settle"] + 120))
        self.b = b
        try:
            if self.verb == "backup":
                self.device, self.unread = identity_of(b, self.family)
                self.expect = self.device
            b.release(ttl=p["settle"] + 1200)
            self.port, why = common.esp_port(r.devreg, p["board"], r.tools["esptool"],
                                             r.tools["chip"])
            if why:
                raise common.port_failure(p["board"], why)
        except BaseException:
            self._back(failed=True)
            raise
        return self

    def _back(self, failed):
        """The board given back: waited for when it should come back as `expect`, else only
        its console. After a failure, a board that does not come back hides nothing."""
        p = self.route.p
        try:
            try:
                if self.expect is not None and self.family:
                    return common.wait_back(self.b, self.family, p["settle"])
                self.b.reacquire()
                return None
            except (common.FlashError, broker_client.BrokerError, stateline.StateError):
                if failed:
                    return None
                raise
        finally:
            self.stack.close()

    def __exit__(self, exc_type, *_rest):
        after = self._back(failed=exc_type is not None)
        if self.verb == "backup":
            self.meta["came_back_as"] = after
        elif after is not None:
            self.result["came_back_as"] = after
        if exc_type is None and self.expect is not None and self.family and \
                identity.same(after, self.expect) is not True:
            if self.verb == "backup":
                raise common.FlashError(f"the board came back as {after}, not {self.expect}")
            raise common.FlashError(f"the board came back as {after}, not as the backup "
                                    f"recorded it, {self.expect}")
        return False

    def read(self, raw):
        got = chip.read(self.route.tools, self.port, raw)
        facts = {k: got[k] for k in ("mac", "flash_size", "chip")}
        return got["size"], facts, {"read_blocks": got["read_blocks"], "blocks": got["blocks"],
                                    "device_unread": self.unread,
                                    "app_manager": _manager_in(raw)}

    def refusals(self, meta, size, raw):
        r = self.route
        self.chip = chip.info(r.tools, self.port)
        why = chip.chip_refusals(meta, self.chip)
        if why:
            return why
        # A backup taken before an app manager and its apps were installed would write them
        # away: the chip's table and the backup's are compared first.
        on_chip = common.read_manager(r.tools["esptool"], r.tools["chip"], self.port)
        in_backup = _manager_in(raw)
        if on_chip and not in_backup:
            why = common.manager_refusal(on_chip, r.p["app_manager_overwrite"],
                                         "restoring a backup without one")
            if why:
                return [why]
        self.app_manager = {"chip": esppart.describe(on_chip),
                            "backup": esppart.describe(in_backup)}
        self.recorded = meta
        return []

    def differ(self, raw):
        how = {"app_manager": self.app_manager}
        if not self.route.check_mode:
            # The write itself writes only the blocks that differ: no diff session first.
            return True, how
        how["msg"] = f"would be written to {self.chip['mac']} ({self.chip['flash_size']})"
        # The plan hashes the chip against the backup (the stub's hashes: seconds), so it says
        # how much a restore or a switch would write.
        d = chip.differ(self.route.tools, self.port, raw)
        how.update(differing_blocks=d["differing_blocks"], blocks=d["blocks"])
        return d["differing_blocks"] > 0, how

    def write(self, raw):
        done = chip.write(self.route.tools, self.port, raw)
        # Only a board that was written is waited for, as the backup recorded it (if it did).
        self.expect, self.family = self.recorded.get("device"), self.recorded.get("family")
        return {"written_blocks": done["written_blocks"], "blocks": done["blocks"],
                "write_seconds": done["seconds"]}


    def identify(self):
        mac = self.route.mac
        got = chip.info(self.route.tools, self.port)
        facts = {k: got[k] for k in ("mac", "flash_size", "chip")}
        if _norm(got["mac"]) != _norm(mac):
            raise common.FlashError(f"the chip on {self.route.p['board']}'s port has MAC "
                                    f"{got['mac']}, the registry's is {mac}: not this board. "
                                    "Nothing was erased")
        return facts, got["size"]

    def erase(self):
        # the MAC is read again in the erasing session itself, and the erase refused unless it
        # is the registry's: the port could have changed hands since identify
        done = chip.erase(self.route.tools, self.port, self.route.mac)
        return {"blocks": done["blocks"],
                "msg": f"the whole flash erased ({done['blocks']} blocks, each read erased after); "
                       "the board stays in its ROM loader until it is flashed"}


def _norm(mac):
    return str(mac or "").lower().replace(":", "").replace("-", "")


class EsptoolChip(chip_flow.ChipRoute):
    """An ESP32 through its ROM loader (and an esp-image board's, which is the same chip)."""
    name = "esptool"
    #: backups taken before routes were recorded are this route's
    takes_unrouted = True

    def __init__(self, p, check_mode=False):
        super().__init__(p)
        self.check_mode = check_mode

    def prepare(self, verb):
        if not self.p["chip"]:
            raise common.FlashError("the board's chip is not known: its type (the registry's "
                                    "board_tag) is not in catalog/boards.yml, so esptool cannot "
                                    "be told what it is")
        self.tools = chip.tools_of(self.p["tools"], self.p["chip"])
        self.devreg = os.path.expanduser(self.p["tools"].get("devreg") or "~/.devreg/devreg.py")
        self.mac = self.p.get("mac")
        if verb == "erase" and not self.mac:
            raise common.FlashError("the registry holds no MAC for this chip (policy.chip_mac, or "
                                    "a usb_serial that is the MAC): nothing proves which chip is "
                                    "on the port, and a whole-chip erase is not made on a guess")

    def same_chip(self, meta, chip_now):
        return not chip.chip_refusals(meta, chip_now)

    def session(self, verb):
        return _Session(self, verb)


def backup(module, p):
    """chip_backup for a board esptool writes."""
    chip_flow.backup(module, p, EsptoolChip(p, module.check_mode))


def restore(module, p, path):
    """chip_restore for a board esptool writes: the blocks that differ from the backup."""
    chip_flow.restore(module, p, EsptoolChip(p, module.check_mode), path)


def erase(module, p):
    """chip_erase for a board esptool writes: erase_flash in the session that reads the MAC."""
    chip_flow.erase(module, p, EsptoolChip(p, module.check_mode))
