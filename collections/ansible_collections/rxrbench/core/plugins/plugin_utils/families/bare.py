"""The bare family: what it knows beyond its row (base.py lists the questions)."""
from . import base


class Family(base.Family):
    #: A bare chip has no console: the survey reads the chips a debug probe is wired to through
    #: the probe, without halting or resetting them (host/bin/swd_survey.py).
    survey = {"tool": "swd_survey.py", "probes": ["swd-probe"]}

    def survey_tags(self, probe, boards=None):
        out = set()
        for chip in (probe or {}).get("chips") or []:
            if chip.get("uid_matches") is True:
                out.add("swd:uid-matches")
            elif chip.get("uid_matches") is False:
                out.add("swd:uid-differs")
        return out

    def survey_describe(self, probe):
        parts = []
        for chip in (probe or {}).get("chips") or []:
            what = f"{chip.get('name')} ({chip.get('chip')})"
            if chip.get("skipped") or chip.get("error"):
                parts.append(f"{what}: {chip.get('skipped') or chip.get('error')}")
                continue
            size = chip.get("flash_size")
            said = f"{what}: uid {chip.get('uid')}, {chip.get('idcode')}" + (
                f", {size // 1024} KB flash" if isinstance(size, int) else "")
            if chip.get("uid_matches") is False:
                said += f", NOT the registry's {chip.get('registry_uid')}"
            elif chip.get("registry_uid") is None:
                said += ", no uid in the registry yet"
            parts.append(said)
        return "; ".join(parts)
