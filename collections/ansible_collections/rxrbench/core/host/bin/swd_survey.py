#!/usr/bin/env python3
"""What is behind a debug probe, for the survey: each chip the registry wires to it, read
through it without halting or resetting (`swd_chip.py id --look`), its unique id set beside the
registry's. Family `bare`'s survey tool (familyspec `survey.probes`).

    swd_survey.py --probe <registry name> --wired '[{"name", "chip", "uid", "nrst"}, ...]'

Prints one JSON object: {"probe", "location", "chips": [{name, chip, uid, registry_uid,
uid_matches, idcode, flash_size, voltage, dpidr} or {name, chip, error} or {name, chip,
skipped}]}. The probe is handed to openocd by its USB location (`devreg resolve --busid`), as
every SWD session is. A chip declared `nrst` is reached only by connecting under reset, which a
survey does not do: it is skipped and says so.

Standard library only; runs beside swd_chip.py on a bench host.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# The probe's USB vendor -> the kind swd_chip drives (an ST-Link is ST's; the rest are CMSIS-DAP).
KIND_BY_VENDOR = {"0483": "stlink"}


def call(argv, timeout):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def last_line(out):
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probe", required=True)
    ap.add_argument("--wired", required=True)
    ap.add_argument("--devreg", default=os.path.expanduser("~/.devreg/devreg.py"))
    ap.add_argument("--swd-chip", default=os.path.join(HERE, "swd_chip.py"))
    ap.add_argument("--timeout", type=int, default=60)
    a = ap.parse_args(argv)
    out = {"probe": a.probe, "chips": []}
    rc, ident = call([sys.executable, a.devreg, "resolve", a.probe], 30)
    rc2, where = call([sys.executable, a.devreg, "resolve", "--busid", a.probe], 30)
    if rc or rc2:
        out["error"] = f"devreg cannot find the probe {a.probe}: {last_line(where or ident)}"
        print(json.dumps(out))
        return 1
    out["location"] = last_line(where)
    kind = KIND_BY_VENDOR.get(last_line(ident).partition(":")[0].lower(), "cmsis-dap")
    for w in json.loads(a.wired):
        chip = {"name": w.get("name"), "chip": w.get("chip")}
        if w.get("nrst"):
            chip["skipped"] = ("reached only by connecting under reset (nrst), which resets it: "
                               "a survey does not")
        elif not w.get("chip"):
            chip["error"] = "the registry names no chip (reach.swd.chip)"
        else:
            rc, got = call([sys.executable, a.swd_chip, "id", "--look", "--chip", w["chip"],
                            "--probe", kind, "--location", out["location"]], a.timeout)
            try:
                read = json.loads(last_line(got))
            except ValueError:
                read = {"error": last_line(got)[-200:] or f"rc {rc}"}
            if "error" in read:
                chip["error"] = read["error"]
            else:
                chip.update({k: read.get(k) for k in ("uid", "idcode", "flash_size", "voltage",
                                                      "dpidr")})
                chip["registry_uid"] = (w.get("uid") or "").lower() or None
                chip["uid_matches"] = (chip["registry_uid"] == (read.get("uid") or "").lower()
                                       if chip["registry_uid"] else None)
        out["chips"].append(chip)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
