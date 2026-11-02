"""What the survey found on a chip that the layout does not own: the plan
reports it from the facts, never from the chip (docs/ARCHITECTURE.md#partitions-and-the-write-rule). Its
documentation is chip_findings.yml: Ansible reads a sidecar only when the .py holds none."""
from ansible_collections.rxrbench.core.plugins.module_utils import site


def _family_view(board, partitions):
    """The board's family's answer about the chip (Family.chip_regions), or the generic one."""
    family = ((board or {}).get("firmware") or {}).get("family")
    if not family or not partitions:
        return {"owned": [], "describe": ""}
    site.load_env()
    from ansible_collections.rxrbench.core.plugins.plugin_utils import families  # pylint: disable=import-outside-toplevel
    return families.plugin(family).chip_regions(board, partitions)


def chip_findings(facts, allowed=None, board=None):
    """[str]: each region of the recorded inventory that holds other firmware, or data every
    app on a chip with a manager shares, with the survey's date; regions `allowed` names
    (bench_overwrite) are left out, and `[all]` leaves out everything. `board` (the board's
    vars) lets its family leave out what its declaration owns there and say what the chip
    carries, which comes first when anything is found."""
    allowed = list(allowed or [])
    if "all" in allowed:
        return []
    esp = ((facts or {}).get("probes") or {}).get("esptool") or {}
    parts = esp.get("partitions") or {}
    inv = parts.get("inventory") or []
    when = esp.get("gathered") or "on an unknown date"
    view = _family_view(board, parts)
    skip = set(allowed) | set(view.get("owned") or [])
    out = []
    for r in inv:
        if r.get("label") in skip:
            continue
        if r.get("owner") == "foreign":
            out.append(f"{r['label']} at {r['offset']:#x} holds other firmware "
                       f"({r.get('recogniser')}), as surveyed {when}")
        elif r.get("owner") == "shared":
            out.append(f"{r['label']} at {r['offset']:#x} holds data every app on this chip "
                       f"shares (it has an app manager), as surveyed {when}")
    if out and view.get("describe"):
        out.insert(0, f"the chip carries {view['describe']}")
    return out


class FilterModule:
    def filters(self):
        return {"chip_findings": chip_findings}
