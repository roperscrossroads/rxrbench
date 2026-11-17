#!/usr/bin/env python3
"""The hawkBit route's tool on a bench host: what the server knows of a device, and telling it
which image the device should take. Standard library only.

    hawkbit_route.py state      what the server holds of a target; changes nothing
    hawkbit_route.py deliver    make the image a software module and a distribution set if
                                the server has neither, and assign the set to the target
    hawkbit_route.py enroll     create the target with its security token if there is none,
                                set the token when the target holds another

Each verb reads one JSON object on stdin and prints one:

    {"settings": {"url": "http://server:8080", "user": "...", "password": "..."},
     "target": "<controller id>",
     "image": "<file>", "manifest": {"format", "target", "version", "file", ...},   deliver
     "name": "<the image's name on this bench, or null>",                            deliver
     "credential": "<the target's security token>"}                    enroll; state compares it

The login reaches this tool on stdin only. Failure is `{"error": "<why>"}` and exit 1.

The client below speaks hawkBit's Management API (`/rest/v1`, as of hawkBit 1.1). Creating is
idempotent: a module or a set is found by name, version and type, so the same image can be
delivered any number of times, and a version that already has another artifact is refused:
on the server a version names one image.
"""
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

#: The software module and distribution set types an image format is stored as. hawkBit hands
#: a device the module's type as the `part` of a deployment, and device clients choose by it.
TYPES = {"raucb": ("os", "os"), "mcuboot-bin": ("application", "app")}
#: Action states after which the server expects nothing more of the device.
CLOSED = {"finished": "success", "error": "failure", "canceled": "failure"}


class HawkbitError(Exception):
    def __init__(self, method, url, status, body=""):
        self.method, self.url, self.status, self.body = method, url, status, body
        try:
            self.code = json.loads(body).get("errorCode", "")
        except (ValueError, AttributeError):
            self.code = ""
        super().__init__(f"{method} {url}: {status} {self.code or body[:200]}")


class NotFound(HawkbitError):
    pass


class Conflict(HawkbitError):
    pass


class Unreachable(HawkbitError):
    """No HTTP answer at all (connection refused, a timeout)."""


def request(method, url, headers=None, body=None, form=None, raw=False, timeout=30):
    """One HTTP call. `body` is sent as JSON; `form` is (filename, bytes), sent as multipart."""
    h = dict(headers or {})
    data = None
    if form is not None:
        boundary = "----rxrbench" + hashlib.sha256(form[1]).hexdigest()[:16]
        data = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{form[0]}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode() \
            + form[1] + f"\r\n--{boundary}--\r\n".encode()
        h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read()
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        raise {404: NotFound, 409: Conflict}.get(e.code, HawkbitError)(method, url, e.code, text) from None
    except (urllib.error.URLError, OSError) as e:
        raise Unreachable(method, url, 0, str(e)) from None
    if raw:
        return out
    return json.loads(out) if out else None


class Mgmt:
    """The Management API, as much of it as the route needs."""

    def __init__(self, url, user, password, timeout=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._auth = {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}

    def call(self, method, path, body=None, **kw):
        return request(method, f"{self.url}/rest/v1/{path}", self._auth, body, timeout=self.timeout, **kw)

    def target(self, controller_id):
        """The target, or None when the server has none of that id."""
        try:
            return self.call("GET", f"targets/{urllib.parse.quote(controller_id, safe='')}")
        except NotFound:
            return None

    def ensure_target(self, controller_id, token):
        """(created, token set). The target made with the token when the server has none of
        that id; the token of an existing one set when it holds another."""
        t = self.target(controller_id)
        if t is None:
            self.call("POST", "targets", [{"controllerId": controller_id, "name": controller_id,
                                           "securityToken": token}])
            return True, True
        if t.get("securityToken") == token:
            return False, False
        self.call("PUT", f"targets/{urllib.parse.quote(controller_id, safe='')}",
                  {"securityToken": token})
        return False, True

    def _find(self, kind, name, version, type_):
        q = urllib.parse.quote(f"name=={name};version=={version}", safe="=;")
        for e in self.call("GET", f"{kind}?q={q}&limit=50")["content"]:
            if e["name"] == name and e["version"] == version and e["type"] == type_:
                return e
        return None

    def ensure_module(self, name, version, type_, filename, blob):
        """(module id, created). One module with one artifact."""
        want = hashlib.sha256(blob).hexdigest()
        found = self._find("softwaremodules", name, version, type_)
        if found is not None:
            arts = self.call("GET", f"softwaremodules/{found['id']}/artifacts")
            arts = arts["content"] if isinstance(arts, dict) else arts
            if arts and all(a["hashes"]["sha256"] != want for a in arts):
                raise Conflict("ENSURE", f"softwaremodules/{found['id']}", 409,
                               f"the server already has {name} {version} with another artifact: "
                               "a version names one image")
            if arts:
                return found["id"], False
            sm = found["id"]
        else:
            sm = self.call("POST", "softwaremodules", [{"name": name, "version": version, "type": type_}])[0]["id"]
        art = self.call("POST", f"softwaremodules/{sm}/artifacts", form=(filename, blob))
        if art["hashes"]["sha256"] != want:
            raise HawkbitError("POST", f"softwaremodules/{sm}/artifacts", 0, "the server stored another sha256")
        return sm, found is None

    def ensure_set(self, name, version, type_, module_ids):
        """(distribution set id, created)."""
        found = self._find("distributionsets", name, version, type_)
        if found is not None:
            return found["id"], False
        return self.call("POST", "distributionsets", [{
            "name": name, "version": version, "type": type_,
            "modules": [{"id": m} for m in module_ids]}])[0]["id"], True

    def assign(self, set_id, controller_id):
        """The new action's id, or None when the target already has this set assigned."""
        r = self.call("POST", f"distributionsets/{set_id}/assignedTargets",
                      [{"id": controller_id, "type": "forced"}])
        acts = r.get("assignedActions") or []
        return acts[0]["id"] if acts else None

    def last_action(self, controller_id):
        cid = urllib.parse.quote(controller_id, safe="")
        acts = self.call("GET", f"targets/{cid}/actions?limit=1&sort=id:DESC")["content"]
        if not acts:
            return None, {}
        hist = self.call("GET", f"targets/{cid}/actions/{acts[0]['id']}/status?limit=1&sort=id:DESC")["content"]
        return acts[0], (hist[0] if hist else {})

    def assigned(self, controller_id):
        """{name, version} of the distribution set assigned to the target, or None."""
        try:
            ds = self.call("GET", f"targets/{urllib.parse.quote(controller_id, safe='')}/assignedDS")
        except NotFound:
            return None
        return {"name": ds["name"], "version": ds["version"]} if ds else None


def client(settings):
    settings = settings or {}
    if not settings.get("url"):
        raise ValueError("the layout names no server for this route (bench_hawkbit.url)")
    return Mgmt(settings["url"], settings.get("user") or "", settings.get("password") or "",
                timeout=float(settings.get("timeout") or 30))


def state(req):
    """What the server holds of the target (flash/delivery.py says what each key is)."""
    m = client(req.get("settings"))
    t = m.target(req["target"])
    if t is None:
        return {"known": False, "open": False, "result": None, "image": None,
                "detail": f"the server has no target {req['target']!r}"}
    # Whether the target holds the declared token. A login that may not read tokens gets none
    # back, and then the server cannot say.
    enrolled = {}
    if req.get("credential"):
        enrolled = {"enrolled": (t["securityToken"] == req["credential"])
                    if t.get("securityToken") else None}
    action, last = m.last_action(req["target"])
    if action is None:
        return {"known": True, "open": False, "result": None, "image": None, **enrolled,
                "detail": f"target {t['updateStatus']}, nothing assigned yet"}
    text = (last.get("messages") or [""])[0]
    return {"known": True, "open": action["status"] not in CLOSED, **enrolled,
            "result": CLOSED.get(action["status"]), "image": m.assigned(req["target"]),
            "id": action["id"],
            "detail": f"target {t['updateStatus']}, action {action['id']} {action['status']}"
                      + (f": {text}" if text else "")}


def deliver(req):
    m = client(req.get("settings"))
    manifest = req.get("manifest") or {}
    if manifest.get("format") not in TYPES:
        raise ValueError(f"this route carries {', '.join(sorted(TYPES))}; the image is "
                         f"{manifest.get('format')!r}")
    module_type, set_type = TYPES[manifest["format"]]
    # The image's name on the server: what the bench calls it (a staged image's name), else
    # what it is for (a bundle's `compatible`). With its version it names one image.
    name = req.get("name") or manifest.get("target") or os.path.splitext(os.path.basename(req["image"]))[0]
    version = manifest.get("version")
    if not version:
        raise ValueError("the image carries no version to name it by on the server")
    with open(req["image"], "rb") as f:
        blob = f.read()
    if m.target(req["target"]) is None:
        raise ValueError(f"the server has no target {req['target']!r}")
    sm, made = m.ensure_module(name, version, module_type, os.path.basename(req["image"]), blob)
    ds, _ = m.ensure_set(name, version, set_type, [sm])
    action = m.assign(ds, req["target"])
    return {"id": action, "already": action is None, "uploaded": made, "name": name,
            "version": version, "sha256": hashlib.sha256(blob).hexdigest()}


def enroll(req):
    """The target with the device's security token: made when the server has none, its token
    set when it holds another. The token is what the device shows the server when it polls."""
    if not req.get("credential"):
        raise ValueError("no security token to enroll the target with")
    created, changed = client(req.get("settings")).ensure_target(req["target"], req["credential"])
    return {"created": created, "changed": changed}


def main(argv=None):
    verbs = {"state": state, "deliver": deliver, "enroll": enroll}
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in verbs:
        print(json.dumps({"error": f"usage: hawkbit_route.py {'|'.join(verbs)} < request.json"}))
        return 1
    try:
        req = json.load(sys.stdin)
        if not req.get("target"):
            raise ValueError("no target")
        print(json.dumps(verbs[argv[0]](req)))
        return 0
    except Unreachable as e:
        print(json.dumps({"error": f"the server does not answer ({e.body})"}))
    except HawkbitError as e:
        print(json.dumps({"error": e.body if e.status in (0, 409) and not e.code else str(e)}))
    except (ValueError, KeyError, OSError) as e:
        print(json.dumps({"error": str(e)}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
