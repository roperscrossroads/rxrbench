#!/usr/bin/python
"""Bring an ESP32 under Launcher to its declared state, through Launcher's serial console."""

DOCUMENTATION = r"""
module: launcher_apply
short_description: Bring a Launcher board to its declared state (Launcher's version, apps, settings, networks)
description:
  - Reads what Launcher holds through its serial console (entering Launcher from a running app
    when it must, the broker attends the boot banner and the board is reset by the port's
    modem lines), plans the difference to the declaration (C(module_utils/plan.py)) and, unless
    this is a plan, writes it in order and reads the board back until nothing differs.
  - Launcher itself is updated by installing its app part as an app (it copies itself into its
    own partition). An app is installed over the console's raw-byte protocol by the host tool
    C(launcher_serial.py) on the released port; an app already in its slot is known by the
    install record this tool keeps (an app's bytes cannot be read back), so a slot with no
    record is written again. Settings go through C(settings set), networks through C(wifi add).
  - The slot that boots cannot be chosen over the serial console; an install selects what it
    wrote, so the declared boot slot is installed last. When nothing installs it, the step is
    reported under C(needs_webui), a difference, not a failure.
  - Refuses to write without the layout's go-ahead (C(apply_allowed)). Check mode plans only,
    and resets nothing; a board in its app is reported as deferred to the apply, which enters
    Launcher and plans there.
options:
  board:
    description: The board's name (its broker).
    type: str
    required: true
  socket:
    description: The broker's socket, when not the default for the board.
    type: path
  images:
    description: What C(launcher_images) resolved (C(manager), C(apps) with the controller's paths).
    type: dict
    required: true
  store:
    description: The directory on this host the images were staged into.
    type: path
    required: true
  launcher:
    description: The board's C(launcher:) block (apps, boot, boot_to_app, boot_timer, wui_user,
      device, wifi, prune, prune_wifi).
    type: dict
    default: {}
  secrets:
    description: C(wui_password) and C(wifi_psk) (by ssid), never logged or returned.
    type: dict
    default: {}
  records:
    description: The install records from the board's facts, by slot label (sha256, size, image).
    type: dict
    default: {}
  tools:
    description: Where the bench host keeps its tools. Keys C(devreg), C(serial_tool).
    type: dict
    default: {}
  deadline:
    description: Seconds each console command may take.
    type: float
    default: 8.0
  wait:
    description: Seconds to wait for Launcher's console after a reset.
    type: float
    default: 30.0
  leave:
    description: Boot the selected app at the end when the board was in its app at the start.
    type: bool
    default: true
  entry:
    description: How the boot banner is answered to stay in Launcher, C(selpress) or C(settings)
      (the board type's C(launcher_enter)); the host tool is told the same.
    type: str
    choices: [selpress, settings]
    default: selpress
  modem_rest:
    description: Where the port's DTR and RTS rest, C(asserted) or C(deasserted) (the board
      type's C(modem_rest)); the host tool opens the released port that way.
    type: str
    choices: [asserted, deasserted]
    default: asserted
  apply_allowed:
    description: The layout's go-ahead to write (C(bench_apply_allowed)).
    type: bool
    default: false
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: Launcher as declared
  rxrbench.launcher.launcher_apply:
    board: "{{ bench_board }}"
    images: "{{ board_images }}"
    store: "{{ board_host_store }}/launcher"
    launcher: "{{ launcher }}"
    secrets: "{{ launcher_secrets[bench_board] | default({}) }}"
    records: "{{ board_records }}"
    apply_allowed: "{{ bench_apply_allowed | default(false) | bool }}"
"""

RETURN = r"""
steps:
  description: The steps planned (and, unless a plan, done), one line each, never a secret.
  returned: always
  type: list
needs_webui:
  description: Differences the serial console cannot write (the boot slot).
  returned: always
  type: list
installed:
  description: The install records made, by slot label (sha256, size, image).
  returned: when an app was installed
  type: dict
launcher:
  description: What the board holds after (before, in a plan), without passwords.
  returned: always
  type: dict
diff:
  description: C(before) and C(after).
  returned: when something differs
  type: dict
"""

import json
import os
import sys

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client, gate
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from ansible_collections.rxrbench.launcher.plugins.module_utils import console, plan


def quoted(text):
    """A console token: in double quotes, with Launcher's escapes."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Runner:
    def __init__(self, module, b, p):
        self.module, self.b, self.p = module, b, p
        self.devreg = os.path.expanduser(p["tools"].get("devreg") or "~/.devreg/devreg.py")
        self.tool = os.path.expanduser(p["tools"].get("serial_tool") or "~/rxrbench/bin/launcher_serial.py")
        self.installed = {}
        self.entered = False

    def ok(self, out, what):
        if not str(out).startswith("OK"):
            raise console.ConsoleError(f"{self.p['board']}: {what}: {out}")

    def run(self, step):
        k, b, declared = step["kind"], self.b, self.p["declared"]
        if k == "settings":
            console.set_settings(b, step["changes"], self.p["deadline"])
        elif k == "wui_password":
            console.set_settings(b, {"wui_pwd": declared["wui_password"]}, self.p["deadline"])
        elif k in ("wifi_add", "wifi_replace", "wifi_del"):
            net = next((n for n in declared.get("wifi") or [] if n["ssid"] == step["ssid"]), {})
            if k != "wifi_add":
                self.ok(console.exec_line(b, f"wifi del {quoted(step['ssid'])}", self.p["deadline"]),
                        f"wifi del {step['ssid']}")
            if k != "wifi_del":
                if net.get("psk") is None:
                    raise console.ConsoleError(f"{self.p['board']}: no password declared for network "
                                               f"{step['ssid']} (launcher_secrets)")
                self.ok(console.exec_line(b, f"wifi add {quoted(step['ssid'])} {quoted(net['psk'])}",
                                          self.p["deadline"]), f"wifi add {step['ssid']}")
        elif k == "prune":
            self.ok(console.exec_line(b, f"partition delete {step['label']}", self.p["deadline"]),
                    f"delete slot {step['label']}")
        elif k == "manager":
            img = step["image"]
            self.install("launch", img["path"], img["app_offset"], img["app_size"], overwrite=False)
            # the new copy boots from its slot, copies itself into Launcher's partition and
            # reboots; a power-on then lands in it
            self.reenter()
        elif k == "install":
            if step.get("replace"):
                self.ok(console.exec_line(b, f"partition delete {step['label']}", self.p["deadline"]),
                        f"delete slot {step['label']} to reinstall it")
            self.install(step["label"], step["path"], step["app_offset"], step["app_size"],
                         overwrite=step["overwrite"])
            self.installed[step["label"]] = {"sha256": step["sha256"], "size": step["size"],
                                             "image": step["image"]}
            self.reenter()
        else:
            raise console.ConsoleError(f"{self.p['board']}: unknown step {k!r}")

    def install(self, name, path, offset, length, overwrite):
        """The host tool on the released, guarded port; the broker gets the port back after."""
        self.b.release(ttl=self.p["wait"] + 600)
        try:
            port, why = common.guarded_port(self.devreg, self.p["board"])
            if why:
                raise common.FlashError(why)
            argv = [sys.executable, self.tool, "--port", port, "--name", name, "--file", path,
                    "--offset", str(offset), "--length", str(length), "--wait", str(self.p["wait"]),
                    "--entry", self.p.get("entry", "selpress"), "--modem-rest", self.p.get("modem_rest", "asserted")]
            if overwrite:
                argv.append("--overwrite")
            # the board's whole say during the install, beside its broker's log
            capture = os.path.expanduser(f"~/rxrbench/logs/{self.p['board']}-install-{name}.log")
            if os.path.isdir(os.path.dirname(capture)):
                argv += ["--capture", capture]
            rc, out = common.tool(argv, timeout=900)
            last = next((ln for ln in reversed(out.strip().splitlines()) if ln.startswith("{")), None)
            try:
                r = json.loads(last) if last else {}
            except ValueError:
                r = {}
            if rc != 0 or not r.get("ok"):
                # what the stream got to says where it went wrong: a device that stopped
                # acknowledging part way lost bytes (a UART bridge has no flow control)
                said = ", ".join(f"{k} {r[k]}" for k in ("sent", "acked", "size", "lost_acks", "seconds")
                                 if r.get(k) is not None)
                raise common.FlashError(f"installing {name} failed (rc {rc}): "
                                        f"{r.get('reply') or out.strip()[-300:]}"
                                        + (f" ({said})" if said else "")
                                        + (f"; the board restarted under it "
                                           f"({', '.join(r['resets'])})" if r.get("resets") else "")
                                        + (f"; the board said last: {' | '.join(r['tail'])}"
                                           if r.get("tail") else "")
                                        + (f" (all of it: {capture})" if "--capture" in argv else ""))
        finally:
            self.b.reacquire()

    def reenter(self):
        """Launcher's console again after a step that rebooted the board."""
        console.enter(self.b, wait=self.p["wait"], entry=self.p.get("entry", "selpress"))
        self.entered = True


def main():
    module = AnsibleModule(
        argument_spec={
            "board": {"type": "str", "required": True},
            "socket": {"type": "path"},
            "images": {"type": "dict", "required": True},
            "store": {"type": "path", "required": True},
            "launcher": {"type": "dict", "default": {}},
            "secrets": {"type": "dict", "default": {}, "no_log": True},
            "records": {"type": "dict", "default": {}},
            "tools": {"type": "dict", "default": {}},
            "deadline": {"type": "float", "default": 8.0},
            "wait": {"type": "float", "default": 30.0},
            "leave": {"type": "bool", "default": True},
            "entry": {"type": "str", "choices": list(console.ENTRIES), "default": "selpress"},
            "modem_rest": {"type": "str", "choices": ["asserted", "deasserted"], "default": "asserted"},
            **gate.ARGS,
        },
        supports_check_mode=True,
    )
    p = module.params
    gate.check(module)   # the layout's go-ahead, before any device is touched
    p["declared"] = plan.declared_from(p["images"], p["store"], p["launcher"] or {}, p["secrets"] or {})
    result = {"changed": False, "steps": [], "needs_webui": [], "launcher": {}}
    try:
        with broker_client.Board(p["board"], path=p["socket"], timeout=p["wait"] + 900) as b:
            if module.check_mode and not console.version(b):
                # A plan resets nothing: a board in its app is not brought into Launcher to be
                # read. The apply enters Launcher (a power-on) and plans there.
                result["changed"] = True
                module.exit_json(deferred="the board is in its app, not in Launcher: a plan does not "
                                 "reset it; the apply enters Launcher and plans there", **result)
            try:
                _v, how = console.enter(b, wait=p["wait"], entry=p["entry"])
            except console.ConsoleError as e:
                module.fail_json(msg=str(e), no_launcher=True, **result)
            entered = how == "entered"
            facts = console.read(b, p["deadline"])
            pl = plan.plan(p["declared"], facts, p["records"])
            result.update(steps=[plan.shown(s) for s in pl["steps"]],
                          needs_webui=[plan.shown(s) for s in pl["needs_webui"]],
                          launcher={**facts, "settings": console.public(facts["settings"])},
                          changed=bool(pl["steps"]))
            if pl["steps"] or pl["needs_webui"]:
                result["diff"] = {"before": pl["before"], "after": pl["after"]}
            if module.check_mode or not pl["steps"]:
                if entered and p["leave"]:
                    console.leave(b)
                module.exit_json(**result)
            runner = Runner(module, b, p)
            for step in pl["steps"]:
                runner.run(step)
            # read back: nothing may differ now, the boot slot apart
            after = console.read(b, p["deadline"])
            records = {**p["records"], **runner.installed}
            again = plan.plan(p["declared"], after, records)
            result.update(launcher={**after, "settings": console.public(after["settings"])},
                          installed=runner.installed,
                          needs_webui=[plan.shown(s) for s in again["needs_webui"]])
            result["diff"] = {"before": pl["before"], "after": again["before"]}
            if again["steps"]:
                module.fail_json(msg=f"{p['board']}: not as declared after the write: "
                                     + "; ".join(plan.shown(s) for s in again["steps"]), **result)
            if (entered or runner.entered) and p["leave"]:
                console.leave(b)
    except (broker_client.BrokerError, console.ConsoleError, common.FlashError) as e:
        unanswered = "port closed" in str(e) or "port is not open" in str(e)
        module.fail_json(msg=str(e), unanswered=unanswered, **result)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
