"""How uf2_bootloader reads xiao_otafix.py: what it refused, what it found, what it did."""
import importlib

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common

bf = importlib.import_module("ansible_collections.rxrbench.mesh.plugins.modules.uf2_bootloader")
# the ESP32 MCUboot path, which stayed in the core module
cbf = importlib.import_module("ansible_collections.rxrbench.core.plugins.modules.bootloader_flash")

HEAD = "xiao9: UF2 drive at /media/x\n"
STOCK_061 = (HEAD + "INVENTORY xiao9: bootloader 0.6.1 (Nov 12 2021) | model Seeed XIAO nRF52840 "
             "| board-id Seeed_XIAO_nRF52840_Sense | softdevice S140 version 7.3.0 | image end "
             "0xd2100 (stock app)\n")


class Board:
    def release(self, ttl=0):
        pass

    def reacquire(self):
        return {"ok": True}


def flasher(monkeypatch, transcript, rc=0, stock=True):
    f = bf.Uf2Otafix(Board(), {"family": "zephyrtastic", "board": "xiao9", "otafix": "/x", "devreg": "/d", "app": None,
                              "known_version": None, "settle": 1, "chip_id": "0A0B0C0D0E0F1011",
                              "stage_app": None})
    monkeypatch.setattr(f, "is_stock", lambda: stock)
    monkeypatch.setattr(common, "tool", lambda argv, timeout, env=None: (rc, transcript))
    return f


def test_a_factory_board_is_updated_and_says_so(monkeypatch):
    out = STOCK_061 + ("xiao9: copying the OTAFIX update (30720 B)\nkit9: app back after the "
                       "bootloader update\nkit9: stock app left as it is (no re-flash)\n")
    before, after, wrote, _ = flasher(monkeypatch, out).run()
    assert (before, wrote) == ("0.6.1", True)
    assert after.startswith("0.9.2-OTAFIX2.5 (from the update file")


def test_a_board_already_on_otafix_is_left_and_its_version_read(monkeypatch):
    out = (HEAD + "INVENTORY xiao9: bootloader 0.9.2-OTAFIX2.5 (Jan 1 2025) | model x | board-id"
           " y | softdevice S140 version 7.3.0 | image end 0x9e000\nkit9: already OTAFIX, "
           "nothing to update\nkit9: UF2 Bootloader 0.9.2-OTAFIX2.5 lib/nrfx\nprogrammed; app up\n")
    before, after, wrote, _ = flasher(monkeypatch, out, stock=False).run()
    assert (before, after, wrote) == ("0.9.2-OTAFIX2.5", "0.9.2-OTAFIX2.5", False)


def test_a_stop_from_the_gates_is_a_failure_that_says_nothing_was_written(monkeypatch):
    out = (HEAD + "INVENTORY xiao9: bootloader 0.8.0 (x) | model Seeed XIAO nRF52840 | ...\n"
           "STOP xiao9: not the known-good starting point (0.6.1 / Seeed XIAO nRF52840 / S140 "
           "7.3.0). Nothing written.\n")
    with pytest.raises(common.FlashError, match="nothing written: STOP xiao9: not the known-good"):
        flasher(monkeypatch, out).run()


def test_a_plan_without_a_known_version_says_applying_will_look():
    f = bf.Uf2Otafix(Board(), {"family": "zephyrtastic", "board": "xiao9", "otafix": "/x", "devreg": "/d", "app": None,
                              "known_version": None, "settle": 1, "chip_id": "0A0B0C0D0E0F1011"})
    write, why = f.plan()
    assert write and "not known" in why
    f.p["known_version"] = "0.9.2-OTAFIX2.5"
    assert f.plan() == (False, "already OTAFIX (0.9.2-OTAFIX2.5)")


def test_stock_is_told_by_the_registry_chip_id_not_by_a_tool_printing_a_path(monkeypatch):
    f = bf.Uf2Otafix(Board(), {"family": "zephyrtastic", "board": "board8", "chip_id": "0A0B0C0D0E0F1011", "otafix": "/x"})
    monkeypatch.setattr(bf.os, "listdir", lambda d: [
        "usb-Seeed_Studio_XIAO-BOOT_0A0B0C0D0E0F1011-if00",
        "usb-Zephyr_Project_CDC_ACM_serial_backend_2A2B2C2D2E2F3031-if00"])
    assert f.is_stock()
    f.p["chip_id"] = "2A2B2C2D2E2F3031"
    assert not f.is_stock()


def test_no_chip_id_means_nothing_is_written():
    f = bf.Uf2Otafix(Board(), {"family": "zephyrtastic", "board": "board8", "chip_id": None, "otafix": "/x", "app": None})
    assert "no chip id" in f.refusals()[0]


def test_the_script_is_told_the_small_image_and_decides_whether_it_is_needed(monkeypatch):
    calls = []

    def tool(argv, timeout, env=None):  # pylint: disable=unused-argument
        calls.append(argv)
        return 0, (HEAD + "INVENTORY xiao9: bootloader 0.6.1 (x) | model Seeed XIAO nRF52840 |"
                   " ...\nkit9: the carrier image goes on first\nkit9: copying the OTAFIX update "
                   "(30720 B)\nUF2 Bootloader 0.9.2-OTAFIX2.5 lib\nprogrammed; app up\n")
    f = bf.Uf2Otafix(Board(), {"family": "zephyrtastic", "board": "board8", "otafix": "/x", "app": "/class1.uf2",
                              "chip_id": "0A0B0C0D0E0F1011", "stage_app": "/class12.uf2"})
    monkeypatch.setattr(f, "is_stock", lambda: False)
    monkeypatch.setattr(common, "tool", tool)
    before, after, wrote, _ = f.run()
    assert len(calls) == 1                       # one call: the script flashes the carrier itself
    assert calls[0][-3:] == ["board8", "/class1.uf2", "--carrier=/class12.uf2"]
    assert (before, after, wrote) == ("0.6.1", "0.9.2-OTAFIX2.5", True)


# ---- a layout that declares the bootloader it wants ----------------------------------------
DECLARED = {"family": "zephyrtastic", "board": "board8", "otafix": "/x", "app": "/class1.uf2", "chip_id": "0A0B0C0D0E0F1011",
            "stage_app": "/class12.uf2", "update": "/bl/zt1.uf2",
            "update_sha256": "ab" * 32, "update_version": "0.9.2-OTAFIX2.5-zt1"}


def _declared(monkeypatch, known, outputs):
    calls = []

    def tool(argv, timeout, env=None):  # pylint: disable=unused-argument
        calls.append(argv)
        return outputs[len(calls) - 1]
    f = bf.Uf2Otafix(Board(), {**DECLARED, "known_version": known})
    monkeypatch.setattr(f, "is_stock", lambda: False)
    monkeypatch.setattr(common, "tool", tool)
    return f, calls


def test_a_board_already_on_the_declared_bootloader_is_not_written():
    f = bf.Uf2Otafix(Board(), {**DECLARED, "known_version": "0.9.2-OTAFIX2.5-zt1"})
    assert f.plan() == (False, "already 0.9.2-OTAFIX2.5-zt1")
    assert bf.Uf2Otafix(Board(), {**DECLARED, "known_version": "0.9.2-OTAFIX2.5"}).plan()[0] is True
    assert bf.Uf2Otafix(Board(), {**DECLARED, "known_version": None}).plan()[0] is True


def test_a_declared_update_needs_its_sha_and_its_version(tmp_path, monkeypatch):
    f = bf.Uf2Otafix(Board(), {**DECLARED, "update_sha256": None, "known_version": None,
                              "otafix": str(tmp_path / "missing")})
    monkeypatch.setattr(f, "is_stock", lambda: False)
    why = " ".join(f.refusals())
    assert "needs its sha256 and its version" in why


def test_the_declared_update_is_handed_to_the_script_with_its_sha(monkeypatch):
    f, calls = _declared(monkeypatch, "0.9.2-OTAFIX2.5", [
        (0, HEAD + "INVENTORY b: bootloader 0.9.2-OTAFIX2.5 (x) | model m |\nb: copying the OTAFIX "
         "update (79360 B)\nUF2 Bootloader 0.9.2-OTAFIX2.5-zt1 lib\nprogrammed; app up\n")])
    before, after, wrote, _ = f.run()
    assert len(calls) == 1
    assert "--update=/bl/zt1.uf2" in calls[0] and f"--update-sha256={'ab' * 32}" in calls[0]
    assert "--carrier=/class12.uf2" in calls[0]
    assert (before, after, wrote) == ("0.9.2-OTAFIX2.5", "0.9.2-OTAFIX2.5-zt1", True)


def test_a_board_not_yet_on_otafix_takes_the_pinned_release_then_the_declared_one(monkeypatch):
    f, calls = _declared(monkeypatch, "0.6.1", [
        (0, HEAD + "INVENTORY b: bootloader 0.6.1 (x) | model m |\nb: copying the OTAFIX update "
         "(30720 B)\nUF2 Bootloader 0.9.2-OTAFIX2.5 lib\nprogrammed; app up\n"),
        (0, HEAD + "INVENTORY b: bootloader 0.9.2-OTAFIX2.5 (x) | model m |\nb: copying the OTAFIX "
         "update (79360 B)\nUF2 Bootloader 0.9.2-OTAFIX2.5-zt1 lib\nprogrammed; app up\n")])
    before, after, wrote, _ = f.run()
    assert len(calls) == 2
    assert not any(a.startswith("--update=") for a in calls[0])        # the pinned release first
    assert any(a.startswith("--update=") for a in calls[1])
    assert (before, after, wrote) == ("0.6.1", "0.9.2-OTAFIX2.5-zt1", True)


def test_a_version_not_known_falls_back_to_the_pinned_release_when_it_is_not_otafix(monkeypatch):
    f, calls = _declared(monkeypatch, None, [
        (0, HEAD + "INVENTORY b: bootloader 0.6.1 (x) | model m |\nSTOP b: a custom bootloader only "
         "replaces an OTAFIX one (this is 0.6.1). Nothing written.\n"),
        (0, HEAD + "INVENTORY b: bootloader 0.6.1 (x) | model m |\nb: copying the OTAFIX update "
         "(30720 B)\nUF2 Bootloader 0.9.2-OTAFIX2.5 lib\nprogrammed; app up\n"),
        (0, HEAD + "INVENTORY b: bootloader 0.9.2-OTAFIX2.5 (x) | model m |\nb: copying the OTAFIX "
         "update (79360 B)\nUF2 Bootloader 0.9.2-OTAFIX2.5-zt1 lib\nprogrammed; app up\n")])
    _, after, wrote, _ = f.run()
    assert len(calls) == 3 and after == "0.9.2-OTAFIX2.5-zt1" and wrote


def test_a_stop_from_the_last_step_is_an_error_but_an_earlier_expected_one_is_not(monkeypatch):
    f, _ = _declared(monkeypatch, "0.9.2-OTAFIX2.5", [
        (0, HEAD + "INVENTORY b: bootloader 0.9.2-OTAFIX2.5 (x) | model m |\nSTOP b: the update does "
         "not name this board. Nothing written.\n")])
    with pytest.raises(common.FlashError, match="does not name this board"):
        f.run()


# ---- ESP32: a plan decides from the record, and resets nothing ---------------------------------

WANT = {"kind": "mcuboot", "key": "aa11", "sha256": "f" * 64, "built": "today"}


def test_a_plan_decides_from_the_bootloader_last_read():
    same = {**WANT, "gathered": "2030-01-01T00:00:00Z"}
    write, why = cbf.plan_from_record(WANT, same, "key")
    assert not write and "the same bootloader" in why and "recorded 2030-01-01T00:00:00Z" in why
    write, why = cbf.plan_from_record(WANT, {**same, "key": "bb22", "sha256": "0" * 64}, "key")
    assert write and "another key" in why and "not read now" in why


@pytest.mark.parametrize("known", [None, {}, {"version": "0.9.2"}])
def test_a_plan_with_no_record_says_so_and_leaves_the_read_to_the_apply(known):
    write, why = cbf.plan_from_record(WANT, known, "key")
    assert write and "not known" in why and "read: true" in why


class Exit(Exception):
    pass


class Mod:
    def __init__(self, check_mode):
        self.check_mode, self.result = check_mode, None

    def exit_json(self, **kw):
        self.result = kw
        raise Exit

    def fail_json(self, **kw):
        raise AssertionError(kw)


class Esp:
    want, reads, manager = WANT, 0, None

    def __init__(self, b, p):
        pass

    def refusals(self):
        return []

    def read(self):
        Esp.reads += 1
        return dict(WANT)


@pytest.mark.parametrize("check, read, reads", [(True, False, 0), (True, True, 1), (False, False, 1)])
def test_only_a_plan_that_was_not_asked_to_read_leaves_the_board_alone(monkeypatch, check, read, reads):
    monkeypatch.setattr(cbf, "EspMcuboot", Esp)
    monkeypatch.setattr(cbf.common, "app_running", lambda b, family: False)
    Esp.reads = 0
    mod = Mod(check)
    with pytest.raises(Exit):
        cbf.esp_mcuboot(mod, Board(), {"known": None, "read": read, "policy": "key", "family": "x",
                                      "settle": 1})
    assert Esp.reads == reads and mod.result["read"] is (reads > 0)
    assert mod.result["changed"] is (reads == 0), "no record: a plan says applying will look"


class _Module:
    check_mode = False

    def __init__(self):
        self.params = {"apply_allowed": True}

    def exit_json(self, **kw):
        raise _Done(dict(kw, failed=False))

    def fail_json(self, **kw):
        raise _Done(dict(kw, failed=True))


class _Done(Exception):
    pass


ALREADY = (HEAD + "INVENTORY xiao9: bootloader 0.9.2-OTAFIX2.5-zt1 (Oct 5 2026) | model x | "
           "board-id y | softdevice S140 version 7.3.0 | image end 0xd7900 (stock app)\n"
           "xiao9: already OTAFIX, nothing to update\n"
           "xiao9: nothing written; its own app copied back to leave UF2 mode\n")


def run_update(monkeypatch, back, stock_after=True, answers=False):
    """A board read in UF2 mode and found on OTAFIX: nothing written, then the way back."""
    calls = []

    def tool(argv, timeout, env=None):
        calls.append(argv)
        return (0, ALREADY) if "app-back" not in argv else back
    monkeypatch.setattr(common, "tool", tool)
    # in UF2 mode no app shows; once its app is back, a stock one does
    monkeypatch.setattr(bf.Uf2Otafix, "is_stock",
                        lambda self: stock_after and any("app-back" in c for c in calls))
    monkeypatch.setattr(bf, "app_answers", lambda b, fam, settle: answers)
    p = {"board": "xiao9", "otafix": "/bin/xiao_otafix.py", "devreg": "/d", "app": "/declared.uf2",
         "known_version": None, "settle": 1, "chip_id": "0A0B0C0D0E0F1011", "stage_app": None,
         "family": "zephyrtastic", "update_version": None, "update": None}
    monkeypatch.setattr(bf.os.path, "exists", lambda path: True)
    with pytest.raises(_Done) as e:
        bf.update(_Module(), Board(), p)
    return e.value.args[0], calls


def test_a_board_read_and_left_unchanged_is_given_back_its_own_app(monkeypatch):
    """just boot on a board already on OTAFIX writes nothing; the board, taken to UF2 mode to be
    read, goes back to the app it holds (uf2_flash.py app-back), and is judged stock again."""
    r, calls = run_update(monkeypatch, (0, "xiao9: already in its app at /dev/ttyACM0"))
    assert not r["failed"] and r["changed"] is False and r["stock"] is True
    assert r["after"] == {"version": "0.9.2-OTAFIX2.5-zt1"}, "what the facts record: the one read"
    assert calls[-1][-2:] == ["app-back", "xiao9"] and calls[-1][-3].endswith("/bin/uf2_flash.py")


def test_a_board_that_stays_in_uf2_mode_is_a_failure_that_says_so(monkeypatch):
    r, _ = run_update(monkeypatch, (1, "STOP xiao9: its CURRENT.UF2 holds no app"), stock_after=False)
    assert r["failed"] and "did not go back to its app" in r["msg"]


def test_an_app_of_another_family_counts_as_back(monkeypatch):
    """A board about to switch families still runs the other one: it is up, not missing."""
    r, _ = run_update(monkeypatch, (0, "programmed; app up"), stock_after=False, answers=True)
    assert not r["failed"]


def test_a_plan_that_did_not_read_shows_not_read_rather_than_unknown(monkeypatch):
    """The plan's diff does not show `"bootloader": "unknown"` -> the declared one, which reads as
    a write that will happen: applying reads the board first and writes only if it differs."""
    monkeypatch.setattr(cbf, "EspMcuboot", Esp)
    mod = Mod(True)
    with pytest.raises(Exit):
        cbf.esp_mcuboot(mod, Board(), {"known": None, "read": False, "policy": "key", "family": "x",
                                       "settle": 1, "app_manager_overwrite": False})
    assert mod.result["diff"]["before"]["bootloader"].startswith("not read (applying reads it first")
