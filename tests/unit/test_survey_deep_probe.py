"""The deep probe's facts stand when the board does not answer a known console afterwards
(a board under an app manager boots on into an app): that is recorded as its own finding."""
import importlib.util
import os

import pytest

from tests.unit import esptables

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("usb_survey_deep", os.path.join(
    REPO, "collections", "ansible_collections", "rxrbench", "core", "plugins", "modules", "usb_survey.py"))
survey = importlib.util.module_from_spec(spec)
spec.loader.exec_module(survey)

CHIP = esptables.chip(list(esptables.MANAGED_4MB) + [esptables.row(0, 0x10, 0x160000, 0x100000, "camstr")],
                      images=[0x10000, 0x160000], boot_seq=1)


class Broker:
    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        pass

    def release(self, ttl=0):
        pass

    def reacquire(self):
        pass


def fake_run(argv, _timeout):
    if "read-flash" in argv:
        i = argv.index("read-flash")
        off, n, out = int(argv[i + 1], 16), int(argv[i + 2], 16), argv[i + 3]
        with open(out, "wb") as fh:
            fh.write(CHIP[off:off + n])
        return 0, ""
    if "flash-id" in argv:
        return 0, "Chip is ESP32-D0WD-V3 (revision v3.1)\nMAC: aa:bb:cc:00:11:22\nDetected flash size: 4MB\n"
    return 0, "{}"


@pytest.fixture
def probe(monkeypatch, tmp_path):
    sock = tmp_path / "sock"
    sock.write_text("")
    monkeypatch.setattr(survey, "run", fake_run)
    monkeypatch.setattr(survey.broker_client, "socket_path", lambda _n: str(sock))
    monkeypatch.setattr(survey.broker_client, "Board", Broker)
    monkeypatch.setattr(survey.time, "sleep", lambda _s: None)
    p = {"esp_python": str(tmp_path), "timeout": 5,
         "families": {"fam": {"identity": {"command": "x state"}}}}
    return p, monkeypatch


def test_facts_read_before_a_silent_console_are_kept_and_the_silence_is_recorded(probe):
    p, monkeypatch = probe
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(survey.time, "monotonic", lambda: next(clock))
    def silent(*_a, **_k):
        raise survey.broker_client.BrokerError("no echo")
    monkeypatch.setattr(survey.stateline, "read", silent)
    info = survey.probe_esp("cyd1", "/dev/ttyUSB0", p)
    assert info["partitions"]["boot"] == "camstr"
    assert {r["label"]: r["owner"] for r in info["partitions"]["inventory"]}["test"] == "foreign"
    assert info["came_back"] is False and "app manager boots on" in info["came_back_note"]


def test_a_board_that_answers_after_the_probe_is_not_flagged(probe):
    p, monkeypatch = probe
    monkeypatch.setattr(survey.stateline, "read", lambda *_a, **_k: {"family": "fam"})
    info = survey.probe_esp("cyd1", "/dev/ttyUSB0", p)
    assert "came_back" not in info and info["partitions"]["boot"] == "camstr"
