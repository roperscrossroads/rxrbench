"""How a device is reached, as data from the device registry.

A registry entry's `reach` block says, per bearer (usb, ble, lora, wifi, eth, ssh, swd), whether the
route is `live`, `dormant` or `unproven`, which host has it, and what it can do: `flash`,
`shell`, `config`, `diag`. This module turns that into one chosen route per verb, at
inventory time, so the choice is visible before anything runs.

The rule is deliberately dull:

- the default route is `usb` (for a network device, `ssh`; for a chip behind a debug
  probe, `swd`);
- a layout may pin a route per verb (`routes: {flash: ble}`);
- a pinned route that is not `live`, or cannot do the verb, refuses the layout;
- nothing falls back to another route during a run. `candidates()` returns an ordered list
  so that an opt-in fallback can be added later; today only its first entry is used.

A device with no `reach` block (a simulated board, an older registry entry) gets one live
usb route on its declared host that can do everything.
"""

VERBS = ("shell", "flash", "config", "diag")
DEFAULT_ORDER = ("usb", "ssh", "swd", "ble", "wifi", "eth", "lora")


class RouteError(ValueError):
    pass


def flatten(entry, default_host=None):
    """Every route a registry entry declares: [{bearer, host, state, can, ...}]."""
    reach = (entry or {}).get("reach")
    if not reach:
        return [{"bearer": "usb", "host": default_host, "state": "live", "can": list(VERBS),
                 "synthetic": True}]
    out = []
    for bearer, r in reach.items():
        r = r or {}
        can = set(r.get("can") or [])
        # A usb route's abilities may be stated per mode (app, bootloader, ...): what the
        # route can do is what any of its modes can.
        for mode in (r.get("modes") or {}).values():
            can |= set((mode or {}).get("can") or [])
        assumed = False
        if bearer == "usb" and not can:
            # A usb entry that names its host and nothing else: the wired console is the
            # one route every board has, so it is taken to do everything, and marked.
            can, assumed = set(VERBS), True
        hosts = r.get("hosts") or ([r["host"]] if r.get("host") else [default_host])
        route = {"bearer": bearer, "host": hosts[0], "hosts": hosts,
                 # usb entries predate `state`: a confirmed usb route is live
                 "state": r.get("state") or ("live" if bearer == "usb" else "unproven"),
                 "can": sorted(can)}
        if assumed:
            route["assumed"] = True
        for k in ("addr", "ip", "node", "port", "user", "jump", "tool", "probe", "chip", "uid", "nrst",
                  "identity", "known_hosts"):
            if r.get(k) is not None:
                route[k] = r[k]
        out.append(route)
    return out


def candidates(entry, verb, pin=None, default_host=None):
    """The routes that may carry `verb`, best first. Raises RouteError when a pinned route
    cannot, or when no route can."""
    if verb not in VERBS:
        raise RouteError(f"unknown verb {verb!r}")
    routes = flatten(entry, default_host)
    if pin is not None:
        chosen = [r for r in routes if r["bearer"] == pin]
        if not chosen:
            raise RouteError(f"{verb} is pinned to {pin}, which the registry does not list "
                             f"(it lists {', '.join(r['bearer'] for r in routes)})")
        r = chosen[0]
        if r["state"] != "live":
            raise RouteError(f"{verb} is pinned to {pin}, which is {r['state']}, not live")
        if verb not in r["can"]:
            raise RouteError(f"{verb} is pinned to {pin}, which can only "
                             f"{', '.join(r['can']) or 'do nothing'}")
        return [r]
    order = {b: i for i, b in enumerate(DEFAULT_ORDER)}
    usable = sorted((r for r in routes if r["state"] == "live" and verb in r["can"]),
                    key=lambda r: order.get(r["bearer"], len(order)))
    return usable


def select(entry, pins=None, default_host=None):
    """{verb: route} for every verb the device can be reached for. A verb no live route can
    carry is simply absent; a bad pin raises."""
    pins = pins or {}
    unknown = sorted(set(pins) - set(VERBS))
    if unknown:
        raise RouteError(f"routes: unknown verb(s) {', '.join(unknown)}")
    out = {}
    for verb in VERBS:
        c = candidates(entry, verb, pins.get(verb), default_host)
        if c:
            out[verb] = c[0]
    return out
