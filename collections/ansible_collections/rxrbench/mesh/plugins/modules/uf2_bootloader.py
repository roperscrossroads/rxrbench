#!/usr/bin/python
"""A XIAO nRF52840's UF2 bootloader, as declared: the factory one to OTAFIX, or a rebuilt one."""

DOCUMENTATION = r"""
module: uf2_bootloader
short_description: Bring a XIAO nRF52840's UF2 bootloader to the declared one
description:
  - "Takes the factory UF2 bootloader (0.6.1, S140 7.3.0) to OTAFIX through the bench host's
    gated update tool (C(xiao_otafix.py)), whose gates are kept, not re-derived: any other
    starting point, or an installed image reaching the update's staging area, stops with
    nothing written. On a board running the layout's app the declared app UF2 is re-flashed
    through the new bootloader (the tool's last step); a board on its factory app keeps it for
    the first flash that follows."
  - "A XIAO's bootloader can only be read from inside the bootloader, so a plan uses the
    version already known (C(known_version), from the survey facts or the registry) and says it
    is unknown otherwise."
  - "Never part of C(apply): the bootloader step of C(just boot)."
options:
  board:
    description: The board's name.
    type: str
    required: true
  family:
    description: The firmware family the board runs (a family a plugin registers); how its app is
      read to tell that it answers again after the bootloader write.
    type: str
    required: true
  enter_as:
    description: The family whose way of asking for UF2 mode its app takes, when no family's
      read can say (C(bench_uf2_enter_as), docs/ARCHITECTURE.md#entering-uf2-mode).
    type: str
  app:
    description: the declared app UF2, re-flashed through the new bootloader on
      a board that runs the layout's app.
    type: path
  known_version:
    description: the bootloader version already known for this board.
    type: str
  chip_id:
    description: "the board's FICR chip id (the registry's C(match.usb_serial)):
      how a board on its factory app is told from one on the layout's. Required."
    type: str
  stage_app:
    description: a small app UF2 (the carrier) the script puts on first, only when
      the installed image reaches the bootloader's staging area (0xe0000) and would be
      destroyed by the update. The declared app goes on after the update.
    type: path
  update:
    description: the bootloader update the layout declares (a rebuilt OTAFIX), on the
      bench host. With it the board is brought to C(update_version), through the pinned release
      first when it is not on OTAFIX yet. Needs C(update_sha256) and C(update_version).
    type: path
  update_sha256:
    description: the sha256 of C(update); the script refuses a file that differs.
    type: str
  update_version:
    description: the version C(update) reports about itself (its INFO_UF2.TXT line);
      a board already on it is not written, and one that reads back another is a failure.
    type: str
  otafix:
    description: the bench host's gated bootloader update tool.
    type: path
    default: ~/rxrbench/bin/xiao_otafix.py
  socket:
    description: The broker socket. Default is C($XDG_RUNTIME_DIR/rxrbench/<board>.sock).
    type: path
  devreg:
    description: devreg on this host.
    type: path
    default: ~/.devreg/devreg.py
  settle:
    description: Seconds to wait for the app to answer after a reset.
    type: float
    default: 60.0
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)). Without it the module refuses
      to write; a plan (check mode) needs none.
    type: bool
    default: false
  families:
    description: The device families as data, the inventory's C(bench_families). It says how
      a board's identity is read and what an image of a family must match.
    type: dict
    default: {}
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: The bootloader, XIAO (factory UF2 bootloader to OTAFIX)
  rxrbench.mesh.uf2_bootloader:
    families: "{{ bench_families }}"
    apply_allowed: "{{ bench_apply_allowed | default(false) | bool }}"
    board: "{{ bench_board }}"
    family: "{{ bench_family }}"
    app: "~/rxrbench/store/{{ bench_image | basename }}/app.uf2"
    chip_id: "{{ chip_id }}"
"""

RETURN = r"""
before:
  description: The bootloader version before the run (C(version)).
  returned: always
  type: dict
after:
  description: The bootloader version after a write (C(version)).
  returned: changed
  type: dict
why:
  description: Why it was (or would be) written, or why not.
  returned: always
  type: str
"""

import os
import re

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, familyspec, gate
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, uf2


OTAFIX_VERSION = "0.9.2-OTAFIX2.5"   # the update xiao_otafix.py pins (UPDATE / UPDATE_SHA)


class Uf2Otafix:
    """A UF2 bootloader to OTAFIX, through the bench host's gated update tool."""
    WANT = "OTAFIX"

    def __init__(self, b, p):
        self.b, self.p, self.name = b, p, p["board"]
        self.script = os.path.expanduser(p["otafix"])

    def is_stock(self):
        """A board still on its factory app shows it in its USB product string. Matched on the
        registry's chip id, never on how a tool prints a path: devreg on a bench host answers
        `/dev/ttyACMn`, which carries no chip id."""
        cid = self.p["chip_id"]
        return any("XIAO-BOOT" in n and cid in n for n in os.listdir("/dev/serial/by-id"))

    def refusals(self):
        out = []
        if not re.fullmatch(r"[0-9A-F]{16}", self.p["chip_id"] or ""):
            return ["no chip id for this board (the registry's match.usb_serial): cannot tell "
                    "a factory app from the layout's, so nothing is written"]
        if not os.path.exists(self.script):
            out.append(f"no {self.script} on this host")
        if self.p.get("update"):
            if not (self.p.get("update_sha256") and self.p.get("update_version")):
                out.append("a declared bootloader update needs its sha256 and its version")
            elif not os.path.exists(self.p["update"]):
                out.append(f"the declared bootloader update {self.p['update']} is not on this host")
        if not self.is_stock() and not (self.p["app"] and os.path.exists(self.p["app"])):
            out.append("the board runs the layout's app, and the update re-flashes it: the declared "
                       "app UF2 is needed (app)")
        return out

    def plan(self):
        v = self.p["known_version"]
        want = self.p.get("update_version")
        if want:
            if v == want:
                return False, f"already {want}"
            if v is None:
                return True, ("the bootloader version is not known (no survey or registry record); "
                              f"applying reads it, inside the bootloader, and takes it to {want}")
            return True, f"bootloader {v}: to the declared {want}"
        if v is None:
            return True, "the bootloader version is not known (no survey or registry record); " \
                         "applying reads it, inside the bootloader, and updates only from 0.6.1"
        if self.WANT in v:
            return False, f"already OTAFIX ({v})"
        return True, f"bootloader {v}: the update applies only from 0.6.1, which the run checks"

    def run(self):
        """xiao_otafix.py, console released. Returns (before, after, wrote, transcript)."""
        stock = self.is_stock()
        app = "-" if stock else self.p["app"]
        self.b.release(ttl=900)
        try:
            # An installed image that reaches the bootloader's staging area (0xe0000) is destroyed
            # by the update. The script decides, from what is installed, and when
            # it must put the small carrier image (stage_app, the layout's) on first through the same
            # guarded write, then the declared app after the update: no board is given the carrier
            # it does not need.
            base = ["python3", "-u", self.script, self.name, app]
            if not stock and self.p["stage_app"]:
                base.append(f"--carrier={self.p['stage_app']}")
            declared = [] if stock or not self.p.get("update") else [
                f"--update={self.p['update']}", f"--update-sha256={self.p['update_sha256']}"]
            known = self.p.get("known_version")
            # A declared (rebuilt) bootloader replaces an OTAFIX one only. A board that is not on
            # OTAFIX yet goes through the pinned release first; one whose version is not known is
            # tried with the declared one, and takes the pinned release if the script says it is
            # not OTAFIX.
            env = uf2.tool_env(self.name, self.p["family"], enter_as=self.p.get("enter_as"))  # it drives uf2_flash.py
            out = ""

            def invoke(extra):
                nonlocal out
                rc_, o_ = common.tool(base + extra, 600, env=env)
                out += o_
                return rc_, o_

            if not declared:
                rc, last = invoke([])
            elif known and self.WANT not in known:
                rc, last = invoke([])                       # the pinned release first
                if rc == 0 and "STOP" not in last:
                    rc, last = invoke(declared)
            else:
                rc, last = invoke(declared)
                if not known and "only replaces an OTAFIX one" in last:
                    rc, last = invoke([])                   # it was not OTAFIX after all
                    if rc == 0 and "STOP" not in last:
                        rc, last = invoke(declared)
        finally:
            self.b.reacquire()
        inv = re.search(r"INVENTORY \S+: bootloader (\S+)", out)
        after = re.search(r"UF2 Bootloader (\S+)", out.split("INVENTORY")[-1])
        if "STOP" in last:
            raise common.FlashError("xiao_otafix.py stopped, nothing written: "
                                    + next(ln for ln in last.splitlines() if "STOP" in ln))
        if rc != 0:
            raise common.FlashError(f"xiao_otafix.py failed (rc {rc}): " + out.strip()[-400:])
        wrote = "copying the OTAFIX update" in out
        after_v = after.group(1) if after else None
        if wrote and after_v is None and "app back after the bootloader update" in out:
            # A stock board is not taken back into its bootloader (that would leave it there),
            # so the script cannot print the new version: it is the update file's, which the
            # script pins by sha256. The first flash reads it from inside UF2 mode before it
            # writes.
            after_v = OTAFIX_VERSION + " (from the update file; a stock app hides it)"
        return (inv.group(1) if inv else None, after_v, wrote, out)


    def app_back(self):
        """A run that wrote nothing leaves the board in its bootloader's UF2 mode (it went
        there to read the version): the bench host's UF2 tool copies back the app the board
        holds, which is the reset out of it. (rc, transcript)."""
        tool = os.path.join(os.path.dirname(self.script), "uf2_flash.py")
        self.b.release(ttl=360)
        try:
            return common.tool(["python3", "-u", tool, "app-back", self.name], 300,
                               env=uf2.tool_env(self.name, self.p["family"], enter_as=self.p.get("enter_as")))
        finally:
            self.b.reacquire()


def app_answers(b, family, settle):
    """The app is up: it answers as its declared family, or (a board about to switch
    families) as any family this host knows."""
    if common.came_back(b, family, settle):
        return True
    try:
        return common.read_board_any(b) is not None
    except Exception:  # pylint: disable=broad-except
        return False


def update(module, b, p):
    f = Uf2Otafix(b, p)
    refused = f.refusals()
    if refused:
        module.fail_json(msg="refused: " + "; ".join(refused))
    write, why = f.plan()
    result = {"changed": write, "before": {"version": p["known_version"]}, "why": why}
    if write:
        result["diff"] = {"before": {"bootloader": p["known_version"] or "not read (applying reads it first and writes only if it differs)"},
                          "after": {"bootloader": p.get("update_version") or "0.9.2-OTAFIX2.5"}}
    if not write or module.check_mode:
        module.exit_json(**result)
    before, after, wrote, out = f.run()
    if not wrote:
        after = after or before     # nothing written: the bootloader now is the one read
    result.update({"changed": wrote, "before": {"version": before},
                   "after": {"version": after}, "why": f"updated from {before or 'a version not read'} to {after}" if wrote else
                   f"already OTAFIX ({before})", "stock": f.is_stock()})
    if (after or before or "").find("OTAFIX") < 0:
        module.fail_json(msg=f"the bootloader is not OTAFIX afterwards ({after or before})",
                         transcript=out.splitlines()[-15:], **result)
    if p.get("update_version") and not f.is_stock() and (after or before) != p["update_version"]:
        module.fail_json(msg=f"the bootloader reads back {after or before}, not the declared "
                             f"{p['update_version']}", transcript=out.splitlines()[-15:], **result)
    if not wrote:
        rc, back = f.app_back()
        if rc != 0 or not ("app up" in back or "already in its app" in back):
            module.fail_json(msg="nothing was written, but the board did not go back to its app "
                                 "from UF2 mode: " + back.strip()[-300:], **result)
        result["stock"] = f.is_stock()    # read again: it was read while the board was in UF2 mode
    if not result["stock"] and not app_answers(b, p["family"], p["settle"]):
        module.fail_json(msg="OTAFIX in place, but the app did not answer afterwards",
                         **result)
    module.exit_json(**result)


def main():
    module = AnsibleModule(
        argument_spec={
            "families": dict(familyspec.ARGUMENT),
            "board": {"type": "str", "required": True},
            "family": {"type": "str", "required": True},
            "enter_as": {"type": "str"},
            "app": {"type": "path"},
            "known_version": {"type": "str"},
            "otafix": {"type": "path", "default": "~/rxrbench/bin/xiao_otafix.py"},
            "chip_id": {"type": "str"},
            "stage_app": {"type": "path"},
            "update": {"type": "path"},
            "update_sha256": {"type": "str"},
            "update_version": {"type": "str"},
            "socket": {"type": "path"},
            "devreg": {"type": "path", "default": "~/.devreg/devreg.py"},
            "settle": {"type": "float", "default": 60.0},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    familyspec.load(p["families"])
    try:
        sock = p["socket"] or broker_client.socket_path(p["board"])
        # a board with no broker (stock firmware, no console) is asked on its port itself
        handle = broker_client.Board if os.path.exists(sock) else common.PortConsole
        with handle(p["board"], path=sock) as b:
            update(module, b, p)
    except (common.FlashError, broker_client.BrokerError, OSError) as e:
        msg = str(e)
        module.fail_json(msg=msg if msg.startswith(f"{p['board']}:") else f"{p['board']}: {msg}")


if __name__ == "__main__":
    main()
