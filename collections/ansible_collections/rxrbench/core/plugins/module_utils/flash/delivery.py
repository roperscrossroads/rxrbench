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
    <tool> deliver   {settings, target, image, manifest, name}   -> the delivery it started
    <tool> enroll    {settings, target, credential}          -> the target made, or its credential set

`settings` is the plugin's own (where its server is, how to log in); it reaches the tool on
stdin, never on a command line. A tool that cannot do what it was asked prints
`{"error": "<why>"}` and exits non-zero. `state` changes nothing.

A route may hold a **credential** for each device: what the device shows the route when it asks
(an update server's per-target token). A plugin that wants it declared names the layout variable
holding it (`credential` in its row); a layout that sets it has the route enrolled by the run
(`enroll`, core's `route_enroll` module), and declares the same value in the device's own
config, in its family's vocabulary. Every request then carries it, and `state` says whether the
route holds the device with it (`enrolled`).
"""
import json
import os
import subprocess
import sys

from . import common


class Delivery:
    """One delivering route to one device.

        spec      the route's ROUTE_DELIVERIES row as the layout carries it (bench_delivery):
                  {bearer, tool, formats}, with the plugin's `settings` beside it and the
                  device's `credential` on the route when its layout declares one
        target    the device's address on that route (the registry's, checked by its caller)
        bin_dir   where a bench host keeps the plugins' tools
    """

    def __init__(self, spec, target, bin_dir=common.PLUGIN_BIN):
        self.bearer = spec.get("bearer") or "?"
        self.formats = tuple(spec.get("formats") or ())
        self.settings = spec.get("settings") or {}
        self.credential = spec.get("credential") or None
        self.target = target
        tool = spec.get("tool") or ""
        self.tool = tool if os.path.isabs(tool) else os.path.join(os.path.expanduser(bin_dir), tool)

    def _call(self, verb, request, timeout):
        if not os.path.isfile(self.tool):
            raise common.FlashError(f"{self.bearer}: its tool {self.tool} is not on this host "
                                    "(the plugin's host tools are installed by `just hosts-setup`)")
        try:
            r = subprocess.run([sys.executable, self.tool, verb],
                               input=json.dumps({
                                   "settings": self.settings, "target": self.target,
                                   **({"credential": self.credential} if self.credential else {}),
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
             "detail": str,                             the route's own last word, for a person
             "enrolled": bool | None}   with a credential declared: the route holds the device
                                        with it (None: the route cannot say)
        """
        return self._call("state", {}, timeout)

    def blockers(self, manifest):
        """Why a delivery of this image cannot start now (asked before anything is changed):
        what `refusals` says, a device the route does not know, a delivery still in flight.
        A device the layout declares a credential for is enrolled by the run before its image
        is delivered, so a route that does not know it yet is no reason to refuse."""
        out = self.refusals(manifest)
        if out:
            return out
        known = self.state()
        if not known.get("known"):
            if self.credential is None:
                out.append(f"its {self.bearer} route does not know the target {self.target!r}")
        elif known.get("open"):
            out.append(f"its {self.bearer} route still has a delivery in flight for it "
                       f"({known.get('detail') or 'no detail'})")
        return out

    def enrollment(self):
        """None when the route holds the device as declared, else what `enroll` would do, in
        words. Changes nothing."""
        if self.credential is None:
            return None
        known = self.state()
        if not known.get("known"):
            return f"its {self.bearer} route does not know {self.target!r}: to be enrolled"
        if known.get("enrolled") is False:
            return (f"its {self.bearer} route holds {self.target!r} with another credential: "
                    "to be set")
        return None

    def enroll(self, timeout=60):
        """Make the route know the device with its declared credential: the target made when
        there is none, its credential set when it differs. Returns the tool's record."""
        if self.credential is None:
            raise common.FlashError(f"{self.bearer}: the layout declares no credential for "
                                    f"{self.target} on this route")
        return self._call("enroll", {}, timeout)

    def deliver(self, image, manifest, name=None, timeout=600):
        """Start the delivery of `image` (a file on this host) to the device. `name` is what
        the image is called on this bench (a staged image's name), for a route that keeps
        images by name. Returns the tool's record of it ({"id": ..., "already": bool, ...});
        the device takes the image when it next asks."""
        return self._call("deliver", {"image": image, "manifest": manifest, "name": name}, timeout)

    def verdict(self):
        """For a run that failed after a delivery was started: what the route's own record
        says of it, in words, and whether that is true. The device's answer made the run
        fail; a record that says success beside it is wrong, and is named so."""
        try:
            says = self.state()
        except common.FlashError as e:
            return f"its {self.bearer} route could not be asked what it recorded ({e})"
        detail = says.get("detail") or "no detail"
        if says.get("open"):
            return f"its {self.bearer} route has not heard the outcome yet ({detail})"
        if says.get("result") == "success":
            return f"its {self.bearer} route's record says success: the record is wrong ({detail})"
        if says.get("result") == "failure":
            return f"its {self.bearer} route's record says failure too ({detail})"
        return f"its {self.bearer} route records no outcome ({detail})"

    def record(self, started, image, device):
        """What the route says of the device after a run, beside what the device itself runs
        (`image` and `device` are identities with a `hash`). The device's answer decides
        whether an update worked; `agrees` says whether the route's record matches it, so a
        record that does not is seen. None while the route has not heard the outcome."""
        out = {"bearer": self.bearer, "address": self.target}
        if started is not None:
            out["started"] = started
        try:
            says = self.state()
        except common.FlashError as e:
            out["route_says"], out["agrees"] = {"error": str(e)}, None
            return out
        out["route_says"] = says
        runs = bool(image.get("hash")) and device.get("hash") == image.get("hash")
        if says.get("open") or says.get("result") not in ("success", "failure"):
            out["agrees"] = None
        else:
            out["agrees"] = runs if says["result"] == "success" else not runs
        return out
