"""bootloader_flash's picoboot_mcuboot: MCUboot at the start of an RP2040's or RP2350's flash, read
and written through its ROM bootloader (a fake picotool keeping the flash in a file), identified
by bootid as the staged MCUboot is: its signing key and its hash over the staged length."""
import json
import os

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils import bootid
from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
from ansible_collections.rxrbench.core.plugins.modules import bootloader_flash as bf
from tests.unit.test_picoboot import CHIP, SPEC, chip  # noqa: F401  (the fixture)


def mcuboot(build=b"A"):
    """A made-up MCUboot: its name, a P-256 public key where bootid looks for one, a build mark."""
    return (b"\x00" * 64 + b"MCUboot" + bootid.P256_SPKI + bytes(range(65)) + build * 900)


class Broker:
    released = 0

    def release(self, ttl=0):
        self.released += 1

    def reacquire(self):
        pass


def staged(tmp_path, data, tag="pro-micro"):
    d = tmp_path / "boot"
    d.mkdir(exist_ok=True)
    (d / "mcuboot.bin").write_bytes(data)
    (d / "manifest.json").write_text(json.dumps({"family": "mcuboot", "file": "mcuboot.bin", "offset": "0x0",
                                                 "board_tag": tag}))
    (d / "flash.json").write_text(json.dumps({"map": {"BOOT": "0x0", "BOOT_SIZE": "0x10000"}}))
    return str(d)


def params(tmp_path, tool, image, **kw):
    return {"board": "pog1", "image": image, "board_tag": "pro-micro", "chip_id": CHIP, "uf2": SPEC,
            "enter": ["reset-interface"], "manual_wait": 0, "picotool": tool, "devreg": "/nonexistent", **kw}


def test_mcuboot_is_read_identified_written_in_sectors_and_read_back(chip, tmp_path):  # noqa: F811
    _, tool, set_state, _, flash = chip
    set_state(boot=True)
    want = mcuboot()
    f = bf.PicobootMcuboot(Broker(), params(tmp_path, tool, staged(tmp_path, want)))
    assert f.refusals() == [] and f.want["kind"] == "mcuboot" and f.want["key"]
    before = f.read()
    assert before["kind"] != "mcuboot", "a blank chip holds no MCUboot"
    assert bootid.decide(f.want, before, "key")[0] is True
    set_state(boot=True)
    f.write()
    image = flash.read_bytes()
    assert image[:len(want)] == want and image[len(want):4096 * 2] == b"\xff" * (4096 * 2 - len(want)), \
        "whole sectors: erased flash after the image"
    set_state(boot=True)
    after = f.read()
    assert after["sha256"] == f.want["sha256"] and after["key"] == f.want["key"]
    assert bootid.decide(f.want, after, "exact")[0] is False, "the same MCUboot is not written again"


def test_a_file_that_is_no_mcuboot_or_for_another_board_is_refused(chip, tmp_path):  # noqa: F811
    _, tool, _, _, _ = chip
    f = bf.PicobootMcuboot(Broker(), params(tmp_path, tool, staged(tmp_path, b"\x00" * 3000, tag="other")))
    why = " ".join(f.refusals())
    assert "not an MCUboot" in why and "for other" in why


def test_the_app_is_back_when_devreg_finds_the_board_again(chip, tmp_path, monkeypatch):  # noqa: F811
    _, tool, _, _, _ = chip
    f = bf.PicobootMcuboot(Broker(), params(tmp_path, tool, staged(tmp_path, mcuboot())))
    answers = iter([(None, "absent"), ("/dev/ttyACM0", None)])
    monkeypatch.setattr(common, "guarded_port", lambda devreg, name: next(answers))
    monkeypatch.setattr(bf.time, "sleep", lambda s: None)
    assert f.came_back(5)
