"""Declared files on a Linux device (rxrbench.linux module_utils/files.py, modules/linux_files.py):
compared by digest and mode, written whole, their content never in an output."""
import os
import re
import shlex

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import ssh
from ansible_collections.rxrbench.linux.plugins.module_utils import files
from ansible_collections.rxrbench.linux.plugins.modules import linux_files

CONFIG = "/data/hawkbit/config"
TEXT = "SERVER=http://updates.example.org:8080\nTARGET_TOKEN=made-up-token\n"


class Device:
    """A device as far as these files go: {path: (content, mode)} and what it was asked."""

    def __init__(self, stat=True):
        self.held, self.dirs, self.asked, self.stat = {}, set(), [], stat

    def run(self, command, timeout=30):
        self.asked.append(command)
        path = shlex.split(re.search(r"\[ -f (\S+) \]", command).group(1))[0]
        if path not in self.held:
            return 0, "held none\n"
        content, mode = self.held[path]
        return 0, f"held {files.digest_of(content)} {mode if self.stat else ''}\n"

    def check(self, command, timeout=30):
        self.asked.append(command)
        self.dirs.add(shlex.split(command)[-1])
        return ""

    def write(self, remote, text, mode="600", timeout=30):
        self.held[remote] = (text, mode)


def test_a_file_differs_when_missing_when_its_content_differs_and_when_its_mode_does():
    dev = Device()
    assert files.differs(dev, CONFIG, TEXT, "600") == "missing"
    dev.held[CONFIG] = ("other\n", "600")
    assert files.differs(dev, CONFIG, TEXT, "600") == "content"
    dev.held[CONFIG] = (TEXT, "644")
    assert files.differs(dev, CONFIG, TEXT, "600") == "mode 644"
    dev.held[CONFIG] = (TEXT, "600")
    assert files.differs(dev, CONFIG, TEXT, "0600") is None


def test_a_device_whose_stat_cannot_say_is_compared_by_content_alone():
    dev = Device(stat=False)
    dev.held[CONFIG] = (TEXT, "644")
    assert files.differs(dev, CONFIG, TEXT, "600") is None


def test_what_the_device_holds_is_asked_as_a_digest_and_never_read_by_value():
    dev = Device()
    dev.held[CONFIG] = (TEXT, "600")
    files.differs(dev, CONFIG, TEXT, "600")
    assert all("sha256sum" in c and "cat" not in c for c in dev.asked)
    assert "made-up-token" not in " ".join(dev.asked)


def test_a_write_makes_the_directory_and_sets_the_mode():
    dev = Device()
    files.write(dev, CONFIG, TEXT, "600")
    assert dev.dirs == {"/data/hawkbit"} and dev.held[CONFIG] == (TEXT, "600")


def test_a_device_that_does_not_answer_is_an_error_and_not_a_missing_file():
    class Silent(Device):
        def run(self, command, timeout=30):
            return 255, "ssh: connect to host 192.0.2.9 port 22: No route to host"
    with pytest.raises(ssh.SshError, match="did not say what it holds"):
        files.differs(Silent(), CONFIG, TEXT, "600")


def module(monkeypatch, dev, declared, check=False, allowed=True):
    out = {}

    class Module:
        check_mode = check
        params = {"route": {"bearer": "ssh"}, "files": declared, "apply_allowed": allowed}

        def __init__(self, **_kw):
            pass

        def fail_json(self, **kw):
            out.update(failed=True, **kw)
            raise SystemExit(1)

        def exit_json(self, **kw):
            out.update(kw)
            raise SystemExit(0)

    monkeypatch.setattr(linux_files, "AnsibleModule", Module)
    monkeypatch.setattr(linux_files.ssh, "Target", lambda route: dev)
    with pytest.raises(SystemExit):
        linux_files.main()
    return out


def test_the_module_plans_then_writes_then_changes_nothing(monkeypatch):
    dev, declared = Device(), {CONFIG: {"content": TEXT, "mode": "600"}}
    plan = module(monkeypatch, dev, declared, check=True)
    assert plan == {"changed": True, "steps": [f"{CONFIG}: missing"]} and not dev.held
    assert module(monkeypatch, dev, declared)["changed"] is True and dev.held[CONFIG] == (TEXT, "600")
    assert module(monkeypatch, dev, declared) == {"changed": False, "steps": []}


def test_the_module_never_puts_a_files_content_in_its_output(monkeypatch):
    dev = Device()
    out = module(monkeypatch, dev, {CONFIG: {"content": TEXT}})
    assert "made-up-token" not in repr(out) and dev.held[CONFIG][1] == "600"


def test_the_module_needs_the_go_ahead_and_refuses_a_malformed_declaration(monkeypatch):
    dev = Device()
    assert "bench_apply_allowed" in module(monkeypatch, dev, {CONFIG: {"content": TEXT}}, allowed=False)["msg"]
    assert "whole path" in module(monkeypatch, dev, {"config": {"content": TEXT}})["msg"]
    assert "{content, mode}" in module(monkeypatch, dev, {CONFIG: "text"})["msg"]
    assert not dev.held


def test_the_update_steps_bring_the_files_before_anything_else():
    tasks = os.path.join(os.path.dirname(linux_files.__file__), "..", "..", "roles", "board", "tasks")
    for name in ("update_rauc.yml", "update_linux.yml"):
        text = open(os.path.join(tasks, name), encoding="utf-8").read()
        first = re.search(r"^- name: (.*)$", text, re.M).group(1)
        assert first == "The files the layout declares", (name, first)
