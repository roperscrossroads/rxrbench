"""From what a Launcher board holds to what its layout declares: the steps, in order, and the
difference a plan shows. Pure: no device, no file.

    declared = {"manager": {"version", "image": <images.describe()>} or None,
                "apps": {label: {"image": name, "sha256", "size", "path", "app_offset", "app_size"}},
                "boot": label or None, "prune": bool,
                "settings": {boot_to_app, boot_timer, wui_user, dev_mode, auto_connect: only the keys declared},
                "wui_password": str or None,
                "wifi": [{"ssid", "psk" (or None)}], "prune_wifi": bool}
    facts    = console.read(): version, manager, apps, data, free, boot, settings (with the
               passwords apart under `_wui_password`, `_wifi_passwords`)
    records  = {label: {"sha256", "size", "image"}}: what this tool installed into each slot,
               kept in the board's facts, since an app's bytes cannot be read back

Order of the steps: Launcher itself first (it reboots, and every step after it talks to the
new one), then settings, then saved networks, then the apps (the slot that should boot
last: an install selects the slot it wrote), then what is left over. The boot slot cannot be
set over the serial console: when no install chooses it, the step is reported as needing the
WebUI route (or Launcher's `boot <label>` once it has one), a difference, not a failure.
"""

import json
import os


SETTING_KEYS = {"boot_to_app": "bootToApp", "boot_timer": "bootTimer", "wui_user": "wui_usr",
                "dev_mode": "dev", "auto_connect": "autoConnect"}


def app_state(facts, records):
    """What each installed slot holds, as far as it is known: {label: {size, sha256, image}}."""
    out = {}
    for a in facts.get("apps") or []:
        rec = (records or {}).get(a["label"]) or {}
        out[a["label"]] = {"size": a["size"], "sha256": rec.get("sha256"), "image": rec.get("image")}
    return out


def has_overwrite(version):
    """Does this Launcher's console take `flash overwrite <name>` (reuse a slot)? It came after
    2.9.1 (bmorcelli/Launcher ebb2649, "split flash firmware/overwrite"): a release up to 2.9.1,
    or a console that cannot say its version, has only `flash firmware`, and a slot to reinstall
    is deleted first (`partition delete`, which every release has; 2.9.1 answers a reinstall
    with `ERR unknown command`)."""
    if version in (None, "unknown"):
        return False
    parts = str(version).split(".")
    if all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts) > (2, 9, 1)
    return True     # a beta's commit, from after the split


def plan(declared, facts, records=None):
    steps, needs_webui = [], []
    before = {"version": facts.get("version"), "boot": facts.get("boot"),
              "apps": app_state(facts, records),
              "settings": {k: facts.get("settings", {}).get(k) for k in SETTING_KEYS
                           if k in (declared.get("settings") or {})},
              "wifi": sorted(facts.get("settings", {}).get("wifi") or [])}
    after = {"version": before["version"], "boot": before["boot"], "apps": dict(before["apps"]),
             "settings": dict(before["settings"]), "wifi": list(before["wifi"])}

    # Launcher itself. A build whose console has no `version` (releases up to 2.9.1) cannot
    # say what it is: nothing is compared, and the plan says so rather than update it blind.
    mgr = declared.get("manager")
    unknown = facts.get("version") in (None, "unknown")
    if mgr and mgr.get("version") and unknown:
        needs_webui.append({"kind": "version", "to": mgr["version"]})
    elif mgr and mgr.get("version") and mgr["version"] != facts.get("version"):
        steps.append({"kind": "manager", "from": facts.get("version"), "to": mgr["version"],
                      "image": mgr["image"]})
        after["version"] = mgr["version"]

    # settings
    changes = {}
    for ours, theirs in SETTING_KEYS.items():
        want = (declared.get("settings") or {}).get(ours)
        if want is None:
            continue
        have = facts.get("settings", {}).get(ours)
        if have != want:
            # a flag stays a bool: Launcher skips a 0 or 1 for one (`is<bool>()`)
            changes[theirs] = want
            after["settings"][ours] = want
    if changes:
        steps.append({"kind": "settings", "changes": changes})
    pw = declared.get("wui_password")
    if pw is not None and facts.get("settings", {}).get("_wui_password") != pw:
        steps.append({"kind": "wui_password"})     # the value travels apart from the plan

    # saved networks
    have_psk = facts.get("settings", {}).get("_wifi_passwords") or {}
    declared_ssids = []
    for net in declared.get("wifi") or []:
        ssid, psk = net["ssid"], net.get("psk")
        declared_ssids.append(ssid)
        if ssid not in have_psk:
            steps.append({"kind": "wifi_add", "ssid": ssid})
            after["wifi"] = sorted(set(after["wifi"]) | {ssid})
        elif psk is not None and have_psk.get(ssid) != psk:
            steps.append({"kind": "wifi_replace", "ssid": ssid})
    if declared.get("prune_wifi"):
        for ssid in sorted(set(have_psk) - set(declared_ssids)):
            steps.append({"kind": "wifi_del", "ssid": ssid})
            after["wifi"] = sorted(set(after["wifi"]) - {ssid})

    # apps: the slot that should boot goes last
    installed = {a["label"]: a for a in facts.get("apps") or []}
    boot = declared.get("boot")
    labels = sorted(declared.get("apps") or {}, key=lambda l: (l == boot, l))
    last_install = None
    reuse = has_overwrite(facts.get("version"))
    for label in labels:
        want = declared["apps"][label]
        have, rec = installed.get(label), (records or {}).get(label) or {}
        same = (have is not None and rec.get("sha256") == want["sha256"]
                and have["size"] >= want.get("app_size", want["size"]))
        if same:
            continue
        steps.append({"kind": "install", "label": label, "image": want["image"], "path": want["path"],
                      "sha256": want["sha256"], "size": want["size"],
                      "app_offset": want.get("app_offset", 0), "app_size": want.get("app_size", want["size"]),
                      "overwrite": have is not None and reuse,
                      "replace": have is not None and not reuse,
                      "why": ("not installed" if have is None else
                              "no record of what the slot holds" if not rec.get("sha256") else
                              f"the slot holds {rec.get('image') or rec['sha256'][:12]}")})
        after["apps"][label] = {"size": have["size"] if have else None, "sha256": want["sha256"],
                                "image": want["image"]}
        last_install = label
    if declared.get("prune"):
        for label in sorted(set(installed) - set(declared.get("apps") or {})):
            steps.append({"kind": "prune", "label": label})
            after["apps"].pop(label, None)

    # the boot slot: an install selects what it wrote; anything else needs the WebUI
    if last_install is not None:
        after["boot"] = last_install
    if boot and after["boot"] != boot:
        needs_webui.append({"kind": "boot", "label": boot, "from": after["boot"]})
        after["boot"] = boot
    return {"steps": steps, "needs_webui": needs_webui, "before": before, "after": after}


def shown(step):
    """One step as a line that never carries a secret."""
    k = step["kind"]
    if k == "manager":
        return f"Launcher {step['from']} -> {step['to']}"
    if k == "settings":
        return "settings " + ", ".join(f"{a}={json.dumps(b) if isinstance(b, bool) else b}" for a, b in sorted(step["changes"].items()))
    if k == "wui_password":
        return "WebUI password -> as declared"
    if k in ("wifi_add", "wifi_replace", "wifi_del"):
        return f"{k.replace('_', ' ')} {step['ssid']}"
    if k == "install":
        how = (", reusing the slot" if step["overwrite"] else
               ", the slot deleted first (this Launcher has no `flash overwrite`)"
               if step.get("replace") else "")
        return (f"install {step['image']} into slot {step['label']} ({step['app_size']} B{how}): "
                f"{step['why']}")
    if k == "prune":
        return f"delete slot {step['label']}"
    if k == "boot":
        return f"boot slot {step.get('from')} -> {step['label']}"
    if k == "version":
        return (f"Launcher's version cannot be read on this build (no `version` command before "
                f"main); {step['to']} is declared: compare through the WebUI's /systeminfo")
    return k


def declared_from(images, store, block, secrets):
    """The declaration as `plan()` takes it, from the resolved images (`launcher_images`: the
    controller's paths), the host directory they were staged into, the board's `launcher:`
    block and its secrets ({wui_password, wifi_psk: {ssid: psk}})."""
    def staged(rec):
        return {**rec, "path": os.path.join(store, os.path.basename(rec["path"]))}
    mgr = images.get("manager")
    settings = {k: block[k] for k in SETTING_KEYS
                if block.get(k) is not None}
    psks = (secrets or {}).get("wifi_psk") or {}
    return {"manager": {"version": mgr.get("version"), "image": staged(mgr)} if mgr else None,
            "apps": {label: staged(rec) for label, rec in (images.get("apps") or {}).items()},
            "boot": block.get("boot"), "prune": bool(block.get("prune", False)),
            "settings": settings, "wui_password": (secrets or {}).get("wui_password"),
            "wifi": [{"ssid": n["ssid"], "psk": psks.get(n["ssid"])} for n in block.get("wifi") or []],
            "prune_wifi": bool(block.get("prune_wifi", False))}
