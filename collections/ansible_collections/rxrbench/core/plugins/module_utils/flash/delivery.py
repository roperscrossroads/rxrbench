"""A route that carries an image to the device itself.

Most routes are a way for the bench host to write: a serial port, a debug probe, ssh. Some are
not. An update server holds the image and the device fetches it on its own schedule; the bench
host only says which image the device should take. A plugin brings such a route as a row of
`ROUTE_DELIVERIES` (docs/PLUGINS.md) naming a tool it deploys to a bench host, and a family
that can take a delivered image (`Family.takes_delivery`) builds a `Delivery` from that row and
gives it to its flasher.

What does not change is who says the update worked. The flasher still reads the device before
the write and after it (`device`, `preflight`, `verify` in flash/common.py): a delivery
replaces the write and nothing else, so what the route's own records say of the update is
reported beside the device's answer and never instead of it.

The tool is the plugin's, run here as a program so that neither core nor the family imports
it. It takes one verb as its argument and one JSON object on stdin, and prints one JSON object:

    <tool> state     {settings, target}                      -> what the route knows of the target
    <tool> deliver   {settings, target, image, manifest}     -> the delivery it started

`settings` is the plugin's own (where its server is, how to log in); it reaches the tool on
stdin, never on a command line. A tool that cannot do what it was asked prints
`{"error": "<why>"}` and exits non-zero. `state` changes nothing.
"""
import json
import os
import subprocess
import sys

from . import common


class Delivery:
    """One delivering route to one device.

        spec      the route's ROUTE_DELIVERIES row as the layout carries it (bench_delivery):
                  {bearer, tool, formats}, with the plugin's `settings` beside it
        target    the device's address on that route (the registry's, checked by its caller)
        bin_dir   where a bench host keeps the plugins' tools
    """

    def __init__(self, spec, target, bin_dir=common.PLUGIN_BIN):
        self.bearer = spec.get("bearer") or "?"
        self.formats = tuple(spec.get("formats") or ())
        self.settings = spec.get("settings") or {}
        self.target = target
        tool = spec.get("tool") or ""
        self.tool = tool if os.path.isabs(tool) else os.path.join(os.path.expanduser(bin_dir), tool)

    def _call(self, verb, request, timeout):
        if not os.path.isfile(self.tool):
            raise common.FlashError(f"{self.bearer}: its tool {self.tool} is not on this host "
                                    "(the plugin's host tools are installed by `just hosts-setup`)")
        try:
            r = subprocess.run([sys.executable, self.tool, verb],
                               input=json.dumps({"settings": self.settings, "target": self.target,
                                                 **request}),
                               capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise common.FlashError(f"{self.bearer}: {os.path.basename(self.tool)} {verb}: {e}") from e
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
        except ValueError:
            out = {}
        if r.returncode != 0 or not isinstance(out, dict) or out.get("error"):
            why = (out.get("error") if isinstance(out, dict) else None) or \
                r.stderr.strip()[-300:] or f"exit {r.returncode}"
            raise common.FlashError(f"{self.bearer}: {verb} for {self.target}: {why}")
        return out

    def refusals(self, manifest):
        """Why this route cannot carry the image at all (asked before anything is changed)."""
        if not self.target:
            return [f"its {self.bearer} route records no address for it in the registry"]
        if manifest.get("format") not in self.formats:
            return [f"its flash route {self.bearer} carries {', '.join(self.formats)}; the "
                    f"image is {manifest.get('format')!r}"]
        return []

    def state(self, timeout=60):
        """What the route knows of the device, changing nothing:

            {"known": bool,      the route has this target at all
             "open": bool,       a delivery is still in flight
             "result": None | "success" | "failure",   of the last delivery, as the route has it
             "image": {...} | None,                     the image that delivery was of
             "detail": str}                             the route's own last word, for a person
        """
        return self._call("state", {}, timeout)

    def deliver(self, image, manifest, timeout=600):
        """Start the delivery of `image` (a file on this host) to the device. Returns the
        tool's record of it ({"id": ..., "already": bool, ...}); the device takes the image
        when it next asks."""
        return self._call("deliver", {"image": image, "manifest": manifest}, timeout)
