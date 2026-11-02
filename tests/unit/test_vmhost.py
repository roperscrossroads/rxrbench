"""The simulated bench host's pieces that need no QEMU: the seed it serves, and how it tells
a live machine from a dead one."""
import os
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "collections"))
from ansible_collections.rxrbench.core.sim import vmhost  # noqa: E402  pylint: disable=wrong-import-position


def test_the_seed_server_serves_the_seed_and_nothing_else(tmp_path, monkeypatch):
    # the first boot found the server serving the working directory (the repository, .env and
    # all): the handler's constructor overrides a class attribute named directory
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "meta-data").write_text("instance-id: x\n")
    (tmp_path / "secret").write_text("not served\n")
    monkeypatch.chdir(tmp_path)
    httpd, port = vmhost._serve(str(seed))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/meta-data") as r:
            assert r.read() == b"instance-id: x\n"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/secret")
        assert e.value.code == 404
    finally:
        httpd.shutdown()


def test_a_zombie_is_not_a_running_machine(tmp_path):
    # the owner of a machine that died while coming up can be a zombie; `fresh` must not take
    # it for a running machine and refuse to make a new disk
    proc = subprocess.Popen(["true"])
    while True:
        with open(f"/proc/{proc.pid}/stat") as f:
            if f.read().rpartition(")")[2].split()[0] == "Z":
                break
    pid = tmp_path / "pid"
    pid.write_text(str(proc.pid))
    assert vmhost._alive(str(pid)) is None
    proc.wait()
    pid.write_text(str(os.getpid()))
    assert vmhost._alive(str(pid)) == os.getpid()
    pid.write_text("not a pid")
    assert vmhost._alive(str(pid)) is None
    assert vmhost._alive(str(tmp_path / "absent")) is None


def test_kvm_is_usable_only_when_the_kernel_answers(monkeypatch):
    """A /dev/kvm node that cannot be opened (a WSL2 kernel without nested virtualization: ENODEV)
    or answers a foreign API version is no KVM; QEMU would exit before the machine boots."""
    import errno
    import fcntl
    closed = []
    monkeypatch.setattr(vmhost.os, "close", closed.append)
    monkeypatch.setattr(vmhost.os, "open", lambda *a: 7)
    monkeypatch.setattr(fcntl, "ioctl", lambda fd, req: vmhost.KVM_API_VERSION)
    assert vmhost.have_kvm() is True and closed == [7]
    monkeypatch.setattr(fcntl, "ioctl", lambda fd, req: 3)
    assert vmhost.have_kvm() is False
    monkeypatch.setattr(fcntl, "ioctl", lambda fd, req: (_ for _ in ()).throw(OSError(errno.ENOTTY, "no")))
    assert vmhost.have_kvm() is False

    def enodev(*a):
        raise OSError(errno.ENODEV, "No such device")
    monkeypatch.setattr(vmhost.os, "open", enodev)
    assert vmhost.have_kvm() is False


def test_the_seed_gives_the_host_the_tool_key_and_no_periodic_apt(tmp_path, monkeypatch):
    """unattended-upgrades holds the dpkg lock after a first boot, which fails the install: the
    seed turns periodic apt off and the runcmd stops the timers before cloud-init reports done."""
    import json
    key = tmp_path / "id"
    (tmp_path / "id.pub").write_text("ssh-ed25519 AAAA test\n")
    monkeypatch.setattr(vmhost, "KEY", str(key))
    seed = vmhost._seed("x", str(tmp_path))
    text = (tmp_path / "seed" / "user-data").read_text()
    assert text.startswith("#cloud-config\n")
    cfg = json.loads(text.split("\n", 1)[1])
    assert cfg["users"][0]["ssh_authorized_keys"] == ["ssh-ed25519 AAAA test"]
    assert cfg["package_update"] is False and cfg["package_upgrade"] is False
    assert 'APT::Periodic::Unattended-Upgrade "0";' in cfg["apt"]["conf"]
    assert any("apt-daily-upgrade.timer" in cmd for cmd in cfg["runcmd"])
    assert seed == str(tmp_path / "seed") and (tmp_path / "seed" / "meta-data").exists()


def test_the_cloud_image_is_pinned_here_and_a_different_published_sum_is_refused(tmp_path, monkeypatch):
    """The SHA256SUMS comes from the same server as the image: a cross-check, not the pin."""
    import argparse
    monkeypatch.setattr(vmhost, "STATE", str(tmp_path))
    monkeypatch.setattr(vmhost, "published_hash", lambda: "0" * 64)
    with pytest.raises(vmhost.VmError, match="refused"):
        vmhost.setup(argparse.Namespace(sha256=None))
    assert len(vmhost.IMAGE_SHA256) == 64
