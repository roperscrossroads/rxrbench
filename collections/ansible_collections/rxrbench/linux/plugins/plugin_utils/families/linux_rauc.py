"""The linux_rauc family: Linux with RAUC A/B slots. A signed bundle is written to the
inactive slot over ssh, the device is rebooted, and the slot it comes back on is read. The
health of a slot is RAUC's own word (`boot_status`) unless the image has a gate: the family
that adds one subclasses this."""
from . import linux


class Family(linux.Family):
    #: the families this object answers for (a subclass names its own)
    names = ("linux_rauc",)
    diag_commands = (*linux.Family.diag_commands,
                     {"name": "rauc-status", "cmd": "rauc status --detailed"})
    #: A bundle may also reach the device by a route that carries it itself (an update server
    #: the device polls): the slot it boots is read over ssh all the same.
    takes_delivery = ("raucb",)

    def check_layout(self, all_hostvars):
        """A device that declares a bundle must say where it lands, when its family cannot."""
        out = []
        for name, hv in sorted(all_hostvars.items()):
            if ((hv.get("firmware") or {}).get("family")) not in self.names:
                continue
            facts = self.board_facts(hv, [])
            if hv.get("bench_bundle") and not facts["remote_bundle"]:
                out.append(f"{name}: declares a bundle but not where it lands on the device "
                           "(bench_rauc_remote_bundle): a RAUC image has no standard place")
            wait, soak = hv.get("bench_wait_good") or 0, facts["health_soak"]
            if wait and soak and float(wait) < soak:
                out.append(f"{name}: bench_wait_good is {wait} s, its health gate soaks for "
                           f"{soak} s: the wait would end before the slot can be marked good")
        return out
