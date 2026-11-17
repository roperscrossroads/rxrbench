"""The hawkBit route's tool against a stand-in server: what it reads, what it creates and how
often, and what it says when the server will not have it."""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from ansible_collections.rxrbench.hawkbit.sim import fakeserver

TOOL = os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections",
                    "rxrbench", "hawkbit", "host", "bin", "hawkbit_route.py")
_spec = importlib.util.spec_from_file_location("hawkbit_route", TOOL)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

MANIFEST = {"family": "linux_rauc", "format": "raucb", "target": "example-pi", "version": "1.1",
            "file": "update.raucb"}


@pytest.fixture
def server():
    with fakeserver.FakeHawkbit() as hb:
        hb.settings = {"url": hb.url, "user": "bench", "password": "made-up"}
        yield hb


@pytest.fixture
def bundle(tmp_path):
    p = tmp_path / "update.raucb"
    p.write_bytes(b"a bundle")
    return str(p)


def ask(server, verb, **req):
    return getattr(tool, verb)({"settings": server.settings, "target": "pi-a", **req})


def writes(server):
    return [(m, p) for m, p in server.requests if m != "GET"]


def test_a_target_the_server_does_not_have_is_said_so_and_nothing_is_created(server):
    s = ask(server, "state")
    assert (s["known"], s["open"], s["result"]) == (False, False, None)
    assert "pi-a" in s["detail"] and not writes(server)


def test_a_target_is_created_once_and_keeps_its_token(server):
    assert ask(server, "target", token="first")["created"] is True
    assert ask(server, "target", token="another")["created"] is False
    assert server.targets["pi-a"]["securityToken"] == "first"
    assert ask(server, "state")["known"] is True


def test_a_delivery_uploads_the_image_once_and_assigns_it(server, bundle):
    ask(server, "target", token="t")
    first = ask(server, "deliver", image=bundle, manifest=MANIFEST)
    assert first["id"] and not first["already"] and first["uploaded"]
    assert (first["name"], first["version"]) == ("example-pi", "1.1")
    s = ask(server, "state")
    assert (s["open"], s["result"], s["image"]) == (True, None, {"name": "example-pi", "version": "1.1"})
    del server.requests[:]
    again = ask(server, "deliver", image=bundle, manifest=MANIFEST)
    assert again["already"] and not again["uploaded"]
    assert [p for m, p in writes(server) if "artifacts" in p or p.endswith("softwaremodules")] == []
    assert len(server.modules) == 1 and len(server.sets) == 1 and len(server.actions) == 1


def test_a_version_is_not_given_a_second_image(server, bundle, tmp_path):
    ask(server, "target", token="t")
    ask(server, "deliver", image=bundle, manifest=MANIFEST)
    other = tmp_path / "update.raucb"
    other.write_bytes(b"another bundle under the same version")
    with pytest.raises(tool.Conflict) as e:
        ask(server, "deliver", image=str(other), manifest=MANIFEST)
    assert "a version names one image" in e.value.body


def test_nothing_is_delivered_to_a_target_the_server_does_not_have(server, bundle):
    with pytest.raises(ValueError, match="no target"):
        ask(server, "deliver", image=bundle, manifest=MANIFEST)
    assert not server.modules


def test_an_image_the_route_cannot_carry_is_refused_before_the_server_is_asked(server, bundle):
    with pytest.raises(ValueError, match="carries"):
        ask(server, "deliver", image=bundle, manifest={**MANIFEST, "format": "uf2"})
    assert not server.requests


@pytest.mark.parametrize("finished,result", [("success", "success"), ("failure", "failure")])
def test_the_state_follows_what_the_device_reported(server, bundle, finished, result):
    ask(server, "target", token="t")
    action = ask(server, "deliver", image=bundle, manifest=MANIFEST)["id"]
    device = fakeserver.Device(server.url, "pi-a", "t")
    assert device.poll() == str(action)
    device.fetch()
    device.feedback(action, "proceeding", "none", "installed, rebooting")
    s = ask(server, "state")
    assert (s["open"], s["result"]) == (True, None) and "installed, rebooting" in s["detail"]
    device.feedback(action, "closed", finished, "the device's verdict")
    s = ask(server, "state")
    assert (s["open"], s["result"]) == (False, result) and "the device's verdict" in s["detail"]


def test_reading_the_state_writes_nothing(server, bundle):
    ask(server, "target", token="t")
    ask(server, "deliver", image=bundle, manifest=MANIFEST)
    del server.requests[:]
    ask(server, "state")
    assert server.requests and not writes(server)


def run(verb, request):
    r = subprocess.run([sys.executable, TOOL, verb], input=json.dumps(request), capture_output=True,
                       text=True, timeout=60, check=False)
    return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])


def test_as_a_program_it_reads_one_object_and_prints_one(server):
    rc, out = run("target", {"settings": server.settings, "target": "pi-a", "token": "t"})
    assert (rc, out) == (0, {"created": True})
    rc, out = run("state", {"settings": server.settings, "target": "pi-a"})
    assert rc == 0 and out["known"] is True


def test_as_a_program_a_failure_is_an_error_object_and_a_non_zero_exit(server):
    rc, out = run("state", {"settings": {"url": "http://127.0.0.1:9", "timeout": 2}, "target": "pi-a"})
    assert rc == 1 and "does not answer" in out["error"]
    rc, out = run("state", {"settings": {}, "target": "pi-a"})
    assert rc == 1 and "bench_hawkbit.url" in out["error"]
    rc, out = run("state", {"settings": server.settings})
    assert rc == 1 and "no target" in out["error"]
    rc, out = run("nonsense", {})
    assert rc == 1 and "usage" in out["error"]
