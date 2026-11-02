"""simvm.py setup: the simulated A/B device's pieces are the builds vm/ARTIFACTS.yml records,
by sha256; another build is refused unless asked for."""
import hashlib
import os
import re

import pytest

from ansible_collections.rxrbench.linux.sim import simvm


def test_the_record_names_every_piece_by_a_sha256():
    known = simvm.recorded_hashes()
    assert set(known["image"]) == {"disk.wic", "zImage", "rootfs.ext4"}
    for digest in known["image"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", digest), digest
    assert re.fullmatch(r"20\d\d\.\d\d", known["sandbox"]["u-boot"]["version"])


def test_a_sandbox_binary_is_checked_by_the_version_it_carries(tmp_path, monkeypatch):
    src = tmp_path / "sandbox-src"
    src.mkdir()
    (src / "u-boot").write_bytes(b"\x00\x01U-Boot 2019.04 (Jan 01 2019)\x00")
    (src / "u-boot.dtb").write_bytes(b"dtb")
    assert simvm.version_in(str(src / "u-boot")) == "2019.04"
    monkeypatch.setattr(simvm, "recorded_hashes", lambda: {"sandbox": {"u-boot": {"version": "2026.01"}}, "image": {}})
    state = tmp_path / "state"
    (state / "harness" / ".git").mkdir(parents=True)
    for name in ("STATE", "HARNESS", "SANDBOX", "IMAGE"):
        monkeypatch.setattr(simvm, name, str(state / name.lower()) if name != "STATE" else str(state))
    monkeypatch.setattr(simvm.subprocess, "run", lambda argv, **kw: None)
    monkeypatch.setattr(simvm, "missing", lambda: [])
    with pytest.raises(SystemExit, match="U-Boot 2019.04 is not the recorded 2026.01"):
        simvm.setup(str(src), None)
    (src / "u-boot").write_bytes(b"U-Boot 2026.01 (Oct 15 2026)")
    simvm.setup(str(src), None)


def test_setup_refuses_a_piece_that_is_not_the_recorded_build_unless_told(tmp_path, monkeypatch):
    src = tmp_path / "sandbox-src"
    src.mkdir()
    (src / "u-boot").write_bytes(b"not the recorded binary")
    (src / "u-boot.dtb").write_bytes(b"nor its tree")
    state = tmp_path / "state"
    (state / "harness" / ".git").mkdir(parents=True)        # cloned already: no network
    monkeypatch.setattr(simvm, "STATE", str(state))
    monkeypatch.setattr(simvm, "HARNESS", str(state / "harness"))
    monkeypatch.setattr(simvm, "SANDBOX", str(state / "sandbox"))
    monkeypatch.setattr(simvm, "IMAGE", str(state / "image"))
    calls = []
    monkeypatch.setattr(simvm.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(simvm, "missing", lambda: [])
    with pytest.raises(SystemExit, match="is not the recorded"):
        simvm.setup(str(src), None)
    assert not any(c[0] == "cp" for c in calls), "nothing was copied"
    simvm.setup(str(src), None, unverified=True)
    copied = [c for c in calls if c[0] == "cp"]
    assert [os.path.basename(c[-1]) for c in copied] == ["u-boot", "u-boot.dtb"]


def test_a_piece_with_the_recorded_hash_passes(tmp_path, monkeypatch):
    src = tmp_path / "sandbox-src"
    src.mkdir()
    (src / "u-boot").write_bytes(b"the build this tree runs with")
    (src / "u-boot.dtb").write_bytes(b"and its tree")
    digests = {n: hashlib.sha256((src / n).read_bytes()).hexdigest() for n in ("u-boot", "u-boot.dtb")}
    monkeypatch.setattr(simvm, "recorded_hashes", lambda: {"sandbox": digests, "image": {}})
    state = tmp_path / "state"
    (state / "harness" / ".git").mkdir(parents=True)
    for name in ("STATE", "HARNESS", "SANDBOX", "IMAGE"):
        monkeypatch.setattr(simvm, name, str(state / name.lower()) if name != "STATE" else str(state))
    calls = []
    monkeypatch.setattr(simvm.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(simvm, "missing", lambda: [])
    simvm.setup(str(src), None)
    assert sum(1 for c in calls if c[0] == "cp") == 2


def test_the_environment_the_sandbox_prints_is_read_through_its_colours():
    plain = "boot\n===ENV===\nraucslot=A\nrootpart=2\nBOOT_A_LEFT=3\n"
    assert simvm.printed_env(plain) == {"raucslot": "A", "rootpart": "2", "BOOT_A_LEFT": "3"}
    coloured = ("\x1b[1;36mboot\x1b[0m\r\n===ENV===\r\nraucslot=A\x1b[1;36m\r\nrootpart=2\x1b[1;36m\r\n"
                "BOOT_A_LEFT=\x1b[0m3\r\n")
    assert simvm.printed_env(coloured) == {"raucslot": "A", "rootpart": "2", "BOOT_A_LEFT": "3"}
    assert simvm.printed_env("no mark at all\n") == {}
