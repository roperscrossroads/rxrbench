"""A stand-in for a hawkBit server, for tests and simulated benches.

It answers the part of hawkBit's Management API and device API (DDI) that the route's tool and
a device client use, with the states and JSON shapes of hawkBit 1.1, and it can be told to
misbehave. It is a test double: one tenant, no rollouts, nothing kept across a restart.

    with FakeHawkbit() as hb:           # hb.url is http://127.0.0.1:<port>
        hb.faults["corrupt_download"] = True
        hb.requests                     # [(method, path)] seen so far

    with FakeHawkbit(bind="192.0.2.10", port=8080) as hb:    # where real boards can reach it

On a bench host it can stand in for a server in front of real boards (tests/hw/README.md):
bound to the host's address on the boards' network, it needs no other machine.

`Device` is a device's side of a delivery, for a test that needs one to happen.
"""
import hashlib
import json
import re
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PART = {"os": "os", "application": "bApp", "runtime": "jvm"}


class FakeHawkbit:
    def __init__(self, bind="127.0.0.1", port=0, token_auth=False, polling="00:05:00"):
        self.targets, self.modules, self.sets, self.actions = {}, {}, {}, {}
        self.token_auth = token_auth
        self.polling = polling      # how often a device is told to ask (hh:mm:ss)
        self.requests = []
        # corrupt_download: serve the artifact with one byte changed (the metadata hash stays)
        # fail_feedback:    answer this many feedback posts with 503
        # down:             answer everything with 503
        self.faults = {"corrupt_download": False, "fail_feedback": 0, "down": False}
        self._id = 100
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                with fake._lock:
                    status, out = fake._handle(self.command, self.path, self.headers, body)
                data = out if isinstance(out, bytes) else (json.dumps(out).encode() if out is not None else b"")
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream" if isinstance(out, bytes)
                                 else "application/hal+json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _go

        self._srv = ThreadingHTTPServer((bind, port), Handler)
        self.port = self._srv.server_address[1]
        self.url = f"http://{bind}:{self.port}"

    def __enter__(self):
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self._srv.shutdown()
        self._srv.server_close()

    # --- helpers -----------------------------------------------------------------------------
    def _next(self):
        self._id += 1
        return self._id

    @staticmethod
    def _err(status, code, message):
        return status, {"errorCode": f"hawkbit.server.error.{code}", "message": message}

    def _note(self, action, type_, message):
        action["history"].append({"id": self._next(), "type": type_, "messages": [message]})

    def _open_action(self, cid):
        for a in sorted(self.actions.values(), key=lambda a: -a["id"]):
            if a["target"] == cid and a["active"]:
                return a
        return None

    def _handle(self, method, path, headers, body):
        self.requests.append((method, path.split("?")[0]))
        if self.faults["down"]:
            return 503, None
        path, _, query = path.partition("?")
        host = headers.get("Host") or f"127.0.0.1:{self.port}"
        if path.startswith("/rest/v1/"):
            if not (headers.get("Authorization") or "").startswith("Basic "):
                return 401, None
            return self._mgmt(method, path[len("/rest/v1/"):], query, headers, body)
        m = re.match(r"^/([^/]+)/controller/v1/([^/]+)(/.*)?$", path)
        if m:
            return self._ddi(method, m.group(1), m.group(2), m.group(3) or "", headers, body, host)
        return 404, None

    # --- Management API ---------------------------------------------------------------------
    def _mgmt(self, method, path, query, headers, body):
        js = json.loads(body) if body and "json" in (headers.get("Content-Type") or "") else None
        if path == "system/configs/authentication.targettoken.enabled" and method == "PUT":
            self.token_auth = bool(js["value"])
            return 200, {"value": self.token_auth}
        if path == "targets" and method == "POST":
            out = []
            for t in js:
                if t["controllerId"] in self.targets:
                    return self._err(409, "repo.entityAlreadyExists", "The given entity already exists in database")
                self.targets[t["controllerId"]] = {
                    "controllerId": t["controllerId"], "name": t.get("name", t["controllerId"]),
                    "securityToken": t.get("securityToken", "generated"), "updateStatus": "unknown",
                    "requestAttributes": True}
                out.append(self.targets[t["controllerId"]])
            return 201, out
        m = re.match(r"^targets/([^/]+)$", path)
        if m:
            t = self.targets.get(m.group(1))
            if t is None:
                return self._err(404, "repo.entityNotFound", f"Target with given identifier {{{m.group(1)}}} does not exist.")
            if method == "DELETE":
                del self.targets[m.group(1)]
                return 204, None
            if method == "PUT":
                t.update({k: v for k, v in js.items() if k in ("name", "securityToken")})
            return 200, t
        m = re.match(r"^targets/([^/]+)/assignedDS$", path)
        if m:
            acts = sorted((x for x in self.actions.values() if x["target"] == m.group(1)), key=lambda x: -x["id"])
            if m.group(1) not in self.targets:
                return self._err(404, "repo.entityNotFound", "Target does not exist.")
            if not acts:
                return 204, None
            ds = self.sets[acts[0]["set"]]
            return 200, {"id": ds["id"], "name": ds["name"], "version": ds["version"], "type": ds["type"]}
        m = re.match(r"^targets/([^/]+)/actions$", path)
        if m:
            acts = sorted((a for a in self.actions.values() if a["target"] == m.group(1)), key=lambda a: -a["id"])
            content = [{"id": a["id"], "status": a["status"], "active": a["active"], "type": "update",
                        "forceType": "forced"} for a in acts][:1 if "limit=1" in query else 50]
            return 200, {"content": content, "total": len(acts), "size": len(content)}
        m = re.match(r"^targets/([^/]+)/actions/(\d+)/status$", path)
        if m:
            a = self.actions.get(int(m.group(2)))
            if a is None:
                return self._err(404, "repo.entityNotFound", "Action does not exist.")
            hist = sorted(a["history"], key=lambda h: -h["id"])
            lim = re.search(r"limit=(\d+)", query)
            hist = hist[:int(lim.group(1))] if lim else hist
            return 200, {"content": hist, "total": len(a["history"]), "size": len(hist)}
        m = re.match(r"^actions/(\d+)$", path)
        if m:
            a = self.actions.get(int(m.group(1)))
            return (200, {"id": a["id"], "status": a["status"], "active": a["active"]}) if a else \
                self._err(404, "repo.entityNotFound", "Action does not exist.")
        for kind, store in (("softwaremodules", self.modules), ("distributionsets", self.sets)):
            if path == kind and method == "POST":
                out = []
                for e in js:
                    if any((x["name"], x["version"], x["type"]) == (e["name"], e["version"], e["type"])
                           for x in store.values()):
                        return self._err(409, "repo.entityAlreadyExists", "The given entity already exists in database")
                    i = self._next()
                    store[i] = {"id": i, "name": e["name"], "version": e["version"], "type": e["type"],
                                "modules": e.get("modules", []), "artifacts": []}
                    out.append({k: v for k, v in store[i].items() if k != "artifacts"})
                return 201, out
            if path == kind and method == "GET":
                want = dict(re.findall(r"(name|version)==([^;&]+)", query.replace("%3D", "=").replace("%3B", ";")))
                content = [{k: v for k, v in x.items() if k != "artifacts"} for x in store.values()
                           if all(x[k] == v for k, v in want.items())]
                return 200, {"content": content, "total": len(content), "size": len(content)}
        m = re.match(r"^softwaremodules/(\d+)/artifacts$", path)
        if m:
            sm = self.modules.get(int(m.group(1)))
            if sm is None:
                return self._err(404, "repo.entityNotFound", "SoftwareModule does not exist.")
            if method == "GET":
                return 200, [{k: v for k, v in a.items() if k != "blob"} for a in sm["artifacts"]]
            ctype = headers.get("Content-Type") or ""
            boundary = ctype.split("boundary=")[1].encode()
            part = body.split(b"--" + boundary)[1]
            head, _, blob = part.partition(b"\r\n\r\n")
            blob = blob[:-2] if blob.endswith(b"\r\n") else blob
            name = re.search(rb'filename="([^"]*)"', head).group(1).decode()
            art = {"id": self._next(), "providedFilename": name, "size": len(blob), "blob": blob,
                   "hashes": {"sha1": hashlib.sha1(blob).hexdigest(), "md5": hashlib.md5(blob).hexdigest(),
                              "sha256": hashlib.sha256(blob).hexdigest()}}
            sm["artifacts"].append(art)
            return 201, {k: v for k, v in art.items() if k != "blob"}
        m = re.match(r"^distributionsets/(\d+)/assignedTargets$", path)
        if m and method == "POST":
            ds = self.sets.get(int(m.group(1)))
            if ds is None:
                return self._err(404, "repo.entityNotFound", "DistributionSet does not exist.")
            assigned = []
            for req in js:
                t = self.targets.get(req["id"])
                if t is None:
                    return self._err(404, "repo.entityNotFound", "Target does not exist.")
                old = self._open_action(req["id"])
                if old is not None and old["set"] == ds["id"]:
                    continue
                a = {"id": self._next(), "target": req["id"], "set": ds["id"], "status": "running",
                     "active": True, "history": []}
                self._note(a, "running", "Assignment initiated by user 'admin'")
                self.actions[a["id"]] = a
                t["updateStatus"] = "pending"
                assigned.append({"id": a["id"]})
            return 200, {"alreadyAssigned": len(js) - len(assigned), "assigned": len(assigned),
                         "assignedActions": assigned, "total": len(js)}
        return 404, None

    # --- DDI -------------------------------------------------------------------------------
    def _ddi(self, method, tenant, cid, rest, headers, body, host):
        t = self.targets.get(cid)
        if t is None or (self.token_auth and headers.get("Authorization") != f"TargetToken {t['securityToken']}"):
            return 401, None
        base = f"http://{host}/{tenant}/controller/v1/{cid}"
        a = self._open_action(cid)
        if rest == "":
            if t["updateStatus"] == "unknown":
                t["updateStatus"] = "registered"
            links = {}
            if a is not None:
                links["deploymentBase"] = {"href": f"{base}/deploymentBase/{a['id']}?c=1"}
            if t["requestAttributes"]:
                links["configData"] = {"href": f"{base}/configData"}
            return 200, {"config": {"polling": {"sleep": self.polling}}, "_links": links}
        if rest == "/configData" and method == "PUT":
            t["requestAttributes"] = False
            t["attributes"] = json.loads(body).get("data", {})
            return 200, None
        m = re.match(r"^/deploymentBase/(\d+)$", rest)
        if m and method == "GET":
            a = self.actions.get(int(m.group(1)))
            if a is None or a["target"] != cid:
                return 404, None
            if a["status"] == "running":
                a["status"] = "retrieved"
                self._note(a, "retrieved", "Update Server: Target retrieved update action and should start now the download.")
            chunks = []
            for ref in self.sets[a["set"]]["modules"]:
                sm = self.modules[ref["id"]]
                chunks.append({"part": PART.get(sm["type"], sm["type"]), "version": sm["version"], "name": sm["name"],
                               "artifacts": [{
                                   "filename": x["providedFilename"], "hashes": x["hashes"], "size": x["size"],
                                   "_links": {"download-http": {"href": f"{base}/softwaremodules/{sm['id']}/artifacts/{x['providedFilename']}"},
                                              "md5sum-http": {"href": f"{base}/softwaremodules/{sm['id']}/artifacts/{x['providedFilename']}.MD5SUM"}}}
                                   for x in sm["artifacts"]]})
            return 200, {"id": str(a["id"]), "deployment": {"download": "forced", "update": "forced", "chunks": chunks}}
        m = re.match(r"^/softwaremodules/(\d+)/artifacts/(.+)$", rest)
        if m and method == "GET":
            sm = self.modules.get(int(m.group(1)))
            art = next((x for x in (sm or {}).get("artifacts", []) if x["providedFilename"] == m.group(2)), None)
            if art is None:
                return 404, None
            if a is not None:
                self._note(a, "download", f"Update Server: Target downloads {rest}")
            blob = art["blob"]
            if self.faults["corrupt_download"]:
                blob = blob[:-1] + bytes([blob[-1] ^ 0xFF])
            return 200, blob
        m = re.match(r"^/deploymentBase/(\d+)/feedback$", rest)
        if m and method == "POST":
            if self.faults["fail_feedback"] > 0:
                self.faults["fail_feedback"] -= 1
                return 503, None
            a = self.actions.get(int(m.group(1)))
            if a is None or a["target"] != cid:
                return 404, None
            if not a["active"]:
                return 410, None
            st = json.loads(body)["status"]
            text = (st.get("details") or [""])[0] if st.get("details") else ""
            if st["execution"] == "closed":
                ok = st["result"]["finished"] == "success"
                a["status"], a["active"] = ("finished" if ok else "error"), False
                t["updateStatus"] = "in_sync" if ok else "error"
                self._note(a, a["status"], text)
            else:
                self._note(a, "running", text)
            return 200, None
        return 404, None


class Device:
    """A device's side of one delivery: ask, fetch, check the hash, report."""

    def __init__(self, url, controller_id, token, tenant="DEFAULT"):
        self.base = f"{url.rstrip('/')}/{tenant}/controller/v1/{controller_id}"
        self.headers = {"Authorization": f"TargetToken {token}"}
        self._deployment = None

    def _call(self, method, url, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={**self.headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out = r.read()
        return out if raw else (json.loads(out) if out else None)

    def poll(self):
        """The id of the action the server offers, or None."""
        links = (self._call("GET", self.base).get("_links") or {})
        self._deployment = links.get("deploymentBase", {}).get("href")
        return self._deployment.split("/deploymentBase/")[1].split("?")[0] if self._deployment else None

    def fetch(self):
        """(deployment, artifact bytes); ValueError when the bytes are not what the server
        said they are."""
        dep = self._call("GET", self._deployment)
        art = dep["deployment"]["chunks"][0]["artifacts"][0]
        blob = self._call("GET", art["_links"]["download-http"]["href"], raw=True)
        if hashlib.sha256(blob).hexdigest() != art["hashes"]["sha256"]:
            raise ValueError("sha256 mismatch")
        return dep, blob

    def feedback(self, action, execution, finished="none", detail=""):
        self._call("POST", f"{self.base}/deploymentBase/{action}/feedback", {
            "status": {"execution": execution, "result": {"finished": finished}, "details": [detail]}})
