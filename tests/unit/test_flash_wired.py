"""The real-board flashers against stand-ins for the bench host's tools: every refusal
happens before the console is released, and success is what the board says afterwards."""
import json
import os
import stat

import pytest

from ansible_collections.rxrbench.core.plugins.module_utils.flash import common, esptool, uf2

HASH = "ab" * 32


def script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class FakeBoard:
    """A broker client whose board reports `build`, then (after a flash) `after`."""
    board = "board9"

    def __init__(self, build="old00000", after="new11111", cls=4):
        self.build, self.after, self.cls = build, after, cls
        self.released = 0
        self.log = []

    def exec(self, cmd, deadline=None, dialect=None, **_kw):  # pylint: disable=unused-argument
        import binascii
        if dialect == "meshcore":
            # A Zephyr shell echoes but never answers in MeshCore's "  -> " form.
            from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
            raise broker_client.BrokerError(f"board9: `{cmd}`: timeout")
        j = json.dumps({"v": 1, "build": self.build, "image": "0.4.40+0", "class": self.cls,
                        "board": "heltec_wifi_lora32_v4", "feat": [], "id": "0x1",
                        "managed": 0}, separators=(",", ":"))
        return f"~S{j}*{binascii.crc_hqx(j.encode(), 0):04x}"

    def release(self, ttl=0):  # pylint: disable=unused-argument
        self.released += 1
        return "/dev/ttyACM9"

    def reacquire(self):
        self.build = self.after
        return {"ok": True}


@pytest.fixture
def rig(tmp_path, widgets):  # pylint: disable=unused-argument,redefined-outer-name
    img = tmp_path / "image"
    img.mkdir()
    (img / "app.signed.bin").write_bytes(b"\0" * 1000)
    (img / "flash.json").write_text(json.dumps({
        "images": [{"offset": "0x20000", "file": "app.signed.bin", "role": "slot0"}],
        "partitions": {"slot0": "0x20000", "slot1": "0x220000", "storage": "0x420000"}}))
    manifest = {"family": "widget", "format": "esp-parts", "file": "app.signed.bin",
                "target": "heltec_wifi_lora32_v4", "board_tag": "heltec-v4", "class": 4,
                "version": "0.4.40", "hash": HASH, "build": "new11111"}
    (img / "manifest.json").write_text(json.dumps(manifest))
    tools = {
        "devreg": script(tmp_path / "devreg.py", 'true\n'),   # replaced below: python3 runs it
        "imginfo": str(tmp_path / "imginfo.py"),
        # Every call is logged; a `read-flash` leaves the file it was asked for: that range of
        # the chip a test puts in $ESPTOOL_FLASH, else the table in $ESPTOOL_TABLE, else
        # nothing (no table on the chip).
        "esptool": script(tmp_path / "esptool",
                          'echo "$@" >> "$0.args"; for a; do last=$a; done\n'
                          'case " $* " in *" read-flash "*)\n'
                          '  if [ -n "$ESPTOOL_FLASH" ]; then python3 -c "$READ" "$ESPTOOL_FLASH" "$@"\n'
                          '  else cp "${ESPTOOL_TABLE:-/dev/null}" "$last"; fi;;\n'
                          'esac\n'
                          'echo "Hash of data verified."\n'),
    }
    (tmp_path / "devreg.py").write_text(
        "import sys\nprint('/dev/ttyACM9') if sys.argv[1] == 'resolve' else None\n")
    (tmp_path / "imginfo.py").write_text("print(4)\n")
    return img, manifest, tools, tmp_path


def esp(rig, board, **kw):
    img, manifest, tools, _ = rig
    return esptool.EsptoolFlasher(board, "board2", kw.pop("manifest", manifest),
                                  str(img / "app.signed.bin"), board_tag=kw.pop("board_tag", "heltec-v4"),
                                  declared=kw.pop("declared", {"family": "widget", "class": 4}),
                                  chip=kw.pop("chip", "esp32s3"), psram=kw.pop("psram", "2MB"),
                                  settle=2, **tools, **kw)


def test_a_flash_writes_the_application_slot_only_and_is_judged_by_the_board(widgets, rig):
    board = FakeBoard()
    r = common.drive(esp(rig, board), rig[1])
    assert r["changed"] and r["after"]["build"] == "new11111" and board.released == 1
    args = open(rig[2]["esptool"] + ".args").read().split()
    assert args[-2:] == ["0x20000", str(rig[0] / "app.signed.bin")], "one region: slot0"
    assert "--port" in args and args[args.index("--port") + 1] == "/dev/ttyACM9"
    assert "0x0" not in args, "the bootloader is not rewritten"


def test_a_native_usb_serial_jtag_chip_is_reset_by_watchdog_after_the_write(rig, monkeypatch):
    """The RTS hard reset leaves a chip that was in ROM download mode in ROM."""
    monkeypatch.setattr(common, "usb_face", lambda port, sysfs="/sys": ("303a", "1001", "AA:BB:CC:DD:EE:01"))
    common.drive(esp(rig, FakeBoard()), rig[1])
    args = open(rig[2]["esptool"] + ".args").read().splitlines()[-1].split()
    assert args[args.index("--after") + 1] == "watchdog-reset"


def test_a_uart_bridge_keeps_the_default_reset_after_the_write(rig, monkeypatch):
    monkeypatch.setattr(common, "usb_face", lambda port, sysfs="/sys": ("10c4", "ea60", "0001"))
    common.drive(esp(rig, FakeBoard()), rig[1])
    args = open(rig[2]["esptool"] + ".args").read().splitlines()[-1].split()
    assert "--after" not in args


def test_a_flash_asks_the_chip_for_its_partition_table_before_writing(rig):
    common.drive(esp(rig, FakeBoard()), rig[1])
    calls = [ln.split() for ln in open(rig[2]["esptool"] + ".args").read().splitlines()]
    assert calls[0][calls[0].index("read-flash") + 1:][:2] == ["0x8000", "0xc00"]
    assert calls[0][calls[0].index("--after") + 1] == "no-reset", "the write follows in the same session"
    # no table on this chip (an MCUboot layout): the rule reads the start of each slot the
    # image's map names (docs/ARCHITECTURE.md#partitions-and-the-write-rule), then the write
    heads = [c[c.index("read-flash") + 1:][:2] for c in calls[1:3]]
    assert heads == [["0x20000", "0x40"], ["0x220000", "0x40"]], calls
    assert "write_flash" in calls[3] and len(calls) == 4


CHIP_BLOCKS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "collections", "ansible_collections", "rxrbench", "core", "host", "bin", "chip_blocks.py")

READ = ("import sys; f, a = sys.argv[1], sys.argv[2:]; i = a.index('read-flash'); "
        "o, n = int(a[i + 1], 16), int(a[i + 2], 16); "
        "open(a[i + 3], 'wb').write(open(f, 'rb').read()[o:o + n])")


def app_manager_table(tmp_path, monkeypatch, apps=("camstr", "wifite")):
    """A chip under an app manager with apps installed, each slot holding an image, booting
    the first app (where the board's firmware runs)."""
    from tests.unit import esptables
    rows = list(esptables.MANAGED_4MB) + [
        esptables.row(0, 0x10 + i, 0x160000 + i * 0x100000, 0x100000, a) for i, a in enumerate(apps)]
    path = tmp_path / "chip.bin"
    path.write_bytes(esptables.chip(rows, images=[0x10000] + [0x160000 + i * 0x100000 for i in range(len(apps))],
                                    boot_seq=1))
    monkeypatch.setenv("ESPTOOL_FLASH", str(path))
    monkeypatch.setenv("READ", READ)


def test_a_chip_carrying_an_app_manager_is_not_written_over(rig, monkeypatch):
    """The slot this flasher writes is where the manager keeps its installed apps."""
    app_manager_table(rig[3], monkeypatch)
    board = FakeBoard()
    with pytest.raises(common.FlashError, match="writing the app slot: the write covers test at 0x10000, "
                                                  "which holds an image the layout does not declare"):
        common.drive(esp(rig, board), rig[1])
    calls = [ln.split() for ln in open(rig[2]["esptool"] + ".args").read().splitlines()]
    assert not any("write_flash" in c or "erase-flash" in c for c in calls), "nothing written"
    assert "chip-id" in calls[-1], "the board is booted back out of its loader"
    assert board.released == 1 and board.build == "new11111", "and its console given back"


def test_the_layout_may_say_the_app_manager_is_meant_to_go(rig, monkeypatch):
    app_manager_table(rig[3], monkeypatch)
    f = esp(rig, FakeBoard())
    f.manager_overwrite = True
    assert common.drive(f, rig[1])["changed"]
    assert "write_flash" in open(rig[2]["esptool"] + ".args").read()


def test_a_plan_refuses_from_the_survey_tag_without_touching_the_chip(rig):
    board = FakeBoard()
    f = esp(rig, board)
    f.facts_tags = ["app-manager:test", "app-manager-apps:2", *SAFE]
    with pytest.raises(common.FlashError, match="survey found an app manager in test"):
        common.drive(f, rig[1], check_mode=True)
    assert board.released == 0 and not os.path.exists(rig[2]["esptool"] + ".args")


def test_a_first_flash_reads_the_table_before_it_erases_anything(rig, monkeypatch):
    first_rig(rig)
    app_manager_table(rig[3], monkeypatch)
    board = StockBoard()
    f = esp(rig, board)
    f.facts_tags = SAFE
    with pytest.raises(common.FlashError, match="erasing the chip: the write covers test at 0x10000"):
        common.drive(f, rig[1], first_flash=True)
    calls = open(rig[2]["esptool"] + ".args").read()
    assert "erase-flash" not in calls and "write_flash" not in calls


def test_a_board_that_already_runs_the_image_is_not_touched(rig):
    board = FakeBoard(build="new11111")
    r = common.drive(esp(rig, board), rig[1])
    assert not r["changed"] and board.released == 0
    assert not os.path.exists(rig[2]["esptool"] + ".args")


def test_a_board_that_reports_no_build_is_written_forward_unless_the_layout_says_no(rig):
    """Firmware older than the build id answers as the family's and says too little to compare."""
    board = FakeBoard(build=None)
    for check in (True, False):
        with pytest.raises(common.FlashError, match="bench_flash_unknown is false"):
            common.drive(esp(rig, board), rig[1], check_mode=check, flash_unknown=False)
    assert board.released == 0 and not os.path.exists(rig[2]["esptool"] + ".args")
    r = common.drive(esp(rig, board), rig[1])
    assert r["changed"] and "does not say enough" in r["why"] and r["after"]["build"] == "new11111"
    # A board that says which image it runs is unaffected by the setting.
    assert common.drive(esp(rig, FakeBoard()), rig[1], check_mode=True, flash_unknown=False)["changed"]


def test_an_image_with_no_build_or_hash_is_refused_not_written_blind(rig):
    board = FakeBoard()
    blind = {k: v for k, v in rig[1].items() if k not in ("build", "hash")}
    for check in (True, False):
        with pytest.raises(common.FlashError, match="no build or hash to compare"):
            common.drive(esp(rig, board, manifest=blind), blind, check_mode=check)
    assert board.released == 0 and not os.path.exists(rig[2]["esptool"] + ".args")


def test_a_family_switch_plan_does_not_reset_the_board_to_read_its_bootloader(rig, monkeypatch):
    f = esp(rig, FakeBoard())
    boot = rig[3] / "mcuboot.bin"
    boot.write_bytes(b"x")
    monkeypatch.setattr(f, "_bootloader", lambda: str(boot))
    monkeypatch.setattr(esptool.bootid, "identify", lambda data: {"kind": "mcuboot", "key": "aa11"})
    monkeypatch.setattr(f, "_board_bootloader", lambda: {"kind": "mcuboot", "key": "bb22"})
    assert f.family_switch_refusals({}, check_mode=True) == [] and "not read in a plan" in f.deferred
    (why,) = f.family_switch_refusals({})
    assert "verifies key bb22" in why


def test_check_mode_says_what_would_change_and_writes_nothing(rig):
    board = FakeBoard()
    r = common.drive(esp(rig, board), rig[1], check_mode=True)
    assert r["changed"] and "build is 'old00000', the image has 'new11111'" in r["why"]
    assert board.released == 0


@pytest.mark.parametrize("change, why", [
    (lambda rig: {"board_tag": "heltec-v4r8"}, "for a 'heltec-v4' board; this board is a 'heltec-v4r8'"),
    (lambda rig: {"declared": {"family": "widget", "class": 6}}, "the layout declares class 6"),
    (lambda rig: {"declared": {"family": "widget", "class": 4, "features": ["fast"]}},
     "the image has features"),
    (lambda rig: (rig[3] / "imginfo.py").write_text("print(8)\n") and {}, "the image's bytes are class 8"),
    (lambda rig: (rig[3] / "devreg.py").write_text(
        "import sys\nprint('/dev/ttyACM9')\nsys.exit(3 if sys.argv[1] == 'guard' else 0)\n") and {},
     "devreg guard refuses"),
    (lambda rig: (rig[0] / "app.signed.bin").write_bytes(b"\0" * 0x200001) and {},
     "would reach 0x220000"),
    (lambda rig: (rig[0] / "flash.json").write_text("{}") and {}, "refusing to write blind"),
])
def test_every_refusal_comes_before_the_console_is_released(widgets, rig, change, why):
    board = FakeBoard()
    kw = change(rig)
    with pytest.raises(common.FlashError, match=why):
        common.drive(esp(rig, board, **kw), rig[1])
    assert board.released == 0 and not os.path.exists(rig[2]["esptool"] + ".args")


def test_a_tool_that_says_it_worked_is_not_believed_if_the_board_disagrees(rig):
    board = FakeBoard(after="old00000")       # esptool "verified", the board still runs the old image
    with pytest.raises(common.FlashError, match="does not run the image after the flash"):
        common.drive(esp(rig, board), rig[1])


def test_a_failed_tool_is_an_error_with_its_output(rig):
    script(rig[3] / "esptool", 'echo "A fatal error occurred: no serial data"; exit 2\n')
    with pytest.raises(common.FlashError, match="esptool .*failed .rc 2.*no serial data"):
        common.drive(esp(rig, FakeBoard()), rig[1])


def test_uf2_goes_through_the_uf2_tool_with_the_board_named_as_released(widgets, rig):
    img, manifest, tools, tmp = rig
    (img / "app.uf2").write_bytes(b"UF2\n")
    m = {**manifest, "format": "uf2", "file": "app.uf2", "board_tag": "xiao", "class": 1}
    uf2_tool = script(tmp / "uf2_tool", 'echo "$RXRBENCH_RELEASED $@" > "$0.args"; echo "board7: programmed; app up at /dev/x"\n')
    board = FakeBoard(cls=1)
    f = uf2.Uf2Flasher(board, "board7", m, str(img / "app.uf2"), board_tag="xiao",
                       declared={"family": "widget", "class": 1}, uf2_tool=uf2_tool, devreg=tools["devreg"], settle=2)
    r = common.drive(f, m)
    assert r["changed"] and board.released == 1
    assert open(uf2_tool + ".args").read().split() == ["board7", "uf2", "board7", str(img / "app.uf2")]
    with pytest.raises(common.FlashError, match="this flasher writes uf2"):
        common.drive(uf2.Uf2Flasher(FakeBoard(cls=1), "board7", {**m, "format": "esp-parts"},
                                    str(img / "app.uf2"), uf2_tool=uf2_tool, devreg=tools["devreg"]),
                     m)


def _uf2(rig, board, tool_body, **kw):
    img, manifest, tools, tmp = rig
    (img / "app.uf2").write_bytes(b"UF2\n")
    m = {**manifest, "format": "uf2", "file": "app.uf2", "board_tag": "xiao", "class": 1}
    tool = script(tmp / "uf2_tool", tool_body)
    return m, uf2.Uf2Flasher(board, "board7", m, str(img / "app.uf2"), board_tag="xiao",
                             declared={"family": "widget", "class": 1}, uf2_tool=tool,
                             devreg=tools["devreg"], settle=2, **kw)


def test_the_uf2_tool_is_given_the_way_of_the_family_the_board_was_read_as(widgets, rig):
    import json
    m, f = _uf2(rig, FakeBoard(cls=2), 'echo "board7: programmed; app up"\n')
    r = common.drive(f, m, check_mode=True)
    assert r["changed"] and f.running_family == "widget"
    env = f._env()
    assert [e["family"] for e in json.loads(env["UF2_ENTERS"])] == ["widget"]
    assert env["UF2_RUNS"] == "widget" and "UF2_ENTER_AS" not in env


def test_with_nothing_read_every_familys_way_is_handed_over_the_images_first(monkeypatch):
    from ansible_collections.rxrbench.core.plugins.module_utils import familyspec
    ways = {"a": {"shell_command": "a dfu"}, "b": {"app_tools": ["/x"]}, "c": {"bootloader": "X"},
            "d": {"shell_command": "d dfu"}}
    monkeypatch.setattr(familyspec, "families", lambda: ["a", "b", "c", "d"])
    monkeypatch.setattr(familyspec, "of", lambda f: {"uf2_enter": ways.get(f)})
    assert [e["family"] for e in uf2.enter_entries("d")] == ["d", "a", "b"], "c says nothing of it"
    assert [e["family"] for e in uf2.enter_entries("d", enter_as="b")] == ["b"]
    assert [e["family"] for e in uf2.enter_entries("d", runs="a", enter_as="b")] == ["a"]
    assert uf2.enter_entries("d", runs="c") == [], "a family that says nothing: nobody else's"


def test_a_plan_shows_the_uf2_tools_refusal_before_anything_is_written(widgets, rig):
    m, f = _uf2(rig, FakeBoard(cls=2), 'if [ "$1" = enter-choice ]; then echo "REFUSING: board7 '
                "could be any of 2 families' firmware; bench_uf2_enter_as\"; exit 1; fi; "
                'echo "$1" >> "$0.calls"\n')
    with pytest.raises(common.FlashError, match="enter-choice: REFUSING.*bench_uf2_enter_as"):
        common.drive(f, m, check_mode=True)
    assert not os.path.exists(f.uf2_tool + ".calls"), "the plan sent nothing"


def test_what_the_board_says_it_runs_wins_over_bench_uf2_enter_as(widgets, rig):
    m, f = _uf2(rig, FakeBoard(cls=2), 'echo "board7: programmed; app up"\n', enter_as="gadget")
    with pytest.raises(common.FlashError, match="it says it runs widget, and bench_uf2_enter_as names gadget"):
        common.drive(f, m, check_mode=True)


def test_on_a_first_flash_the_family_named_is_the_only_way_handed_over(widgets, rig):
    import json
    m, f = _uf2(rig, StockBoard(cls=1), 'if [ "$1" = uf2-mode ]; then echo "UF2 Bootloader 0.9.2-OTAFIX2.5"; '
                'else echo "board7: programmed; app up"; fi\n', enter_as="widget")
    f.bootloader_version = "0.9.2-OTAFIX2.5"
    common.drive(f, m, first_flash=True, check_mode=True)
    env = f._env()
    assert f.running_family is None and env["UF2_ENTER_AS"] == "widget" and "UF2_RUNS" not in env
    assert [e["family"] for e in json.loads(env["UF2_ENTERS"])] == ["widget"]


# ---- first flash: a board that does not run the family's firmware ----------------------

class StockBoard(FakeBoard):
    """Answers nothing as the family's firmware until it has been flashed."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.ours = False

    def exec(self, cmd, deadline=None, **kw):
        return super().exec(cmd, deadline, **kw) if self.ours else "uart garbage\n"

    def reacquire(self):
        self.ours = True
        return super().reacquire()


SAFE = ["secure-boot:off", "flash-encryption:off", "rom-download:on", "psram:2MB"]


def first_rig(rig):
    img = rig[0]
    (img / "mcuboot.bin").write_bytes(b"\xe9" + b"\0" * 100)
    fj = json.loads((img / "flash.json").read_text())
    fj["images"].insert(0, {"offset": "0x0", "file": "mcuboot.bin", "role": "bootloader"})
    (img / "flash.json").write_text(json.dumps(fj))


def test_a_board_not_running_our_firmware_is_never_written_without_the_layout_asking(rig):
    first_rig(rig)
    board = StockBoard()
    with pytest.raises(common.FlashError, match="declare bench_first_flash"):
        common.drive(esp(rig, board), rig[1])
    assert board.released == 0


def test_an_esp32_first_flash_erases_the_chip_and_writes_bootloader_then_app(rig):
    first_rig(rig)
    board = StockBoard()
    f = esp(rig, board)
    f.facts_tags = SAFE
    r = common.drive(f, rig[1], first_flash=True)
    assert r["changed"] and r["before"]["family"] == "foreign" and r["after"]["build"] == "new11111"
    calls = [ln.split() for ln in open(rig[2]["esptool"] + ".args").read().splitlines()]
    assert "read-flash" in calls[0] and "0x8000" in calls[0], "the chip's table is asked first"
    assert all("read-flash" in c for c in calls[1:3]), "then the slots of the image's map"
    calls = calls[3:]
    assert "erase-flash" in calls[0]
    assert calls[1][-2:] == ["0x0", str(rig[0] / "mcuboot.bin")] and "keep" in calls[1]
    assert calls[2][-2:] == ["0x20000", str(rig[0] / "app.signed.bin")]


@pytest.mark.parametrize("tags, why", [
    ([], "do not show secure-boot:off, flash-encryption:off, rom-download:on"),
    (SAFE[:3] + ["psram:8MB"], "the chip reports psram:8MB: the wrong board type"),
    (SAFE[:3], "no PSRAM size in the survey facts"),
])
def test_an_esp32_first_flash_needs_the_survey_to_agree(rig, tags, why):
    first_rig(rig)
    board = StockBoard()
    f = esp(rig, board)
    f.facts_tags = tags
    with pytest.raises(common.FlashError, match=why):
        common.drive(f, rig[1], first_flash=True)
    assert board.released == 0 and not os.path.exists(rig[2]["esptool"] + ".args")


@pytest.mark.parametrize("version, ok", [(None, False), ("0.6.1", False),
                                         ("0.9.2-OTAFIX2.5", True)])
def test_a_xiao_first_flash_waits_for_otafix(widgets, rig, version, ok):
    img, manifest, tools, tmp = rig
    (img / "app.uf2").write_bytes(b"UF2\n")
    m = {**manifest, "format": "uf2", "file": "app.uf2", "board_tag": "xiao", "class": 1}
    uf2_tool = script(tmp / "uf2_tool", 'if [ "$1" = uf2-mode ]; then echo "UF2 Bootloader 0.9.2-OTAFIX2.5 lib"; '
                    'else echo "board9: programmed; app up at /dev/x"; fi\n')
    f = uf2.Uf2Flasher(StockBoard(cls=1), "board9", m, str(img / "app.uf2"), board_tag="xiao",
                       declared={"family": "widget", "class": 1}, uf2_tool=uf2_tool, devreg=tools["devreg"], settle=2)
    f.bootloader_version = version
    if ok:
        assert common.drive(f, m, first_flash=True)["changed"]
    else:
        with pytest.raises(common.FlashError, match="run `just boot`"):
            common.drive(f, m, first_flash=True)


def test_a_xiao_first_flash_reads_the_bootloader_itself_before_writing(widgets, rig):
    img, manifest, tools, tmp = rig
    (img / "app.uf2").write_bytes(b"UF2\n")
    m = {**manifest, "format": "uf2", "file": "app.uf2", "board_tag": "xiao", "class": 1}
    uf2_tool = script(tmp / "uf2_tool", 'echo "$1" >> "$0.calls"; if [ "$1" = uf2-mode ]; then '
                    'echo "UF2 Bootloader 0.6.1 lib"; else echo "programmed; app up"; fi\n')
    f = uf2.Uf2Flasher(StockBoard(cls=1), "board9", m, str(img / "app.uf2"), board_tag="xiao",
                       declared={"family": "widget", "class": 1}, uf2_tool=uf2_tool, devreg=tools["devreg"], settle=2)
    f.bootloader_version = "0.9.2-OTAFIX2.5"          # the facts say OTAFIX; the board does not
    with pytest.raises(common.FlashError, match="the bootloader is 0.6.1, not OTAFIX: nothing written"):
        common.drive(f, m, first_flash=True)
    # the plan's question (how it would be asked, nothing sent), then the bootloader read; no write
    assert open(uf2_tool + ".calls").read().split() == ["enter-choice", "uf2-mode"]


def test_a_console_that_does_not_echo_is_not_our_firmware(rig):
    from ansible_collections.rxrbench.core.plugins.module_utils import broker_client

    class Silent(FakeBoard):
        def exec(self, cmd, deadline=None, **_kw):
            raise broker_client.BrokerError("board9: `widget state`: no echo")
    with pytest.raises(common.FlashError, match="declare bench_first_flash"):
        common.drive(esp(rig, Silent()), rig[1])


def test_a_port_that_holds_nothing_is_a_board_that_is_not_there(rig):
    """The broker's port is closed (nothing plugged in behind a bridge the watcher cannot see
    the board by): not another firmware, not a first flash, a board to skip. Said as such,
    and not swallowed by the ask-every-family read either."""
    from ansible_collections.rxrbench.core.plugins.module_utils import broker_client

    class Gone(FakeBoard):
        def exec(self, cmd, deadline=None, **_kw):
            raise broker_client.BrokerError(
                "board9: `widget state`: port closed ([Errno 2] No such file: '/dev/ttyUSB9')")
    with pytest.raises(common.Unanswered, match="its port is not open"):
        common.drive(esp(rig, Gone()), rig[1])
    with pytest.raises(common.Unanswered):
        common.read_board_any(Gone())


def test_a_console_nothing_reads_is_a_board_there_running_nothing_we_know(widgets, rig):
    """A V4 with a fresh MCUboot and another firmware's app in its slot boots nothing that reads
    its USB console: its writes time out, and apply takes the first flash its layout allows
    instead of skipping the board as absent."""
    from ansible_collections.rxrbench.core.plugins.module_utils import broker_client

    class Stuck(FakeBoard):
        def exec(self, cmd, deadline=None, **_kw):
            raise broker_client.BrokerError(f"board9: `{cmd}`: port closed: Write timeout")
    with pytest.raises(common.NotOurs, match="takes no input"):
        common.read_board(Stuck(), "widget")
    with pytest.raises(common.FlashError, match="declare bench_first_flash"):
        common.drive(esp(rig, Stuck()), rig[1])


# ---- a stock app on native USB: esptool needs the ROM download face -----------------------

def _sysfs(tmp_path, tty, vid, pid, serial):
    """A fake sysfs: /sys/class/tty/<tty>/device -> a USB device directory with its ids."""
    usb = tmp_path / "sys" / "devices" / "usb1" / "1-1"
    usb.mkdir(parents=True)
    (usb / "idVendor").write_text(vid + "\n")
    (usb / "idProduct").write_text(pid + "\n")
    (usb / "serial").write_text(serial + "\n")
    iface = usb / "1-1:1.0" / "tty" / tty
    iface.mkdir(parents=True)
    cls = tmp_path / "sys" / "class" / "tty" / tty
    cls.mkdir(parents=True)
    os.symlink(str(iface), str(cls / "device"))
    return str(tmp_path / "sys")


def test_a_stock_app_on_native_usb_is_taken_into_rom_mode_first(rig, tmp_path, monkeypatch):
    from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
    img, manifest, tools, root = rig
    sysfs = _sysfs(tmp_path, "ttyACM9", "303a", "0002", "AABBCCDDEE01")
    want = common.rom_port_path("AA:BB:CC:DD:EE:01")
    # the fake esptool's first run (chip-id, --after no-reset) "resets the chip": the ROM
    # by-id appears. It is a path under /dev the test cannot create, so rom_port_path is
    # pointed at the temp dir for the test.
    rom = tmp_path / "rom-port"
    monkeypatch.setattr(common, "rom_port_path", lambda mac: str(rom) if mac == "AA:BB:CC:DD:EE:01" else "/nonexistent")
    (root / "esptool").write_text("#!/bin/sh\necho \"$@\" >> \"$0.args\"\ncase \" $* \" in *chip-id*) touch " + str(rom) + ";; esac\n")
    port, why = common.esp_port(tools["devreg"], "board2", tools["esptool"], "esp32s3", sysfs=sysfs, wait=5)
    assert why is None and port == str(rom)
    args = open(tools["esptool"] + ".args").read()
    assert "--port /dev/ttyACM9 --after no-reset chip-id" in args, "the app port was only used to reset the chip"
    # the ROM port was guarded by name before it was handed back
    assert open(root / "devreg.py").read()  # (the fake devreg accepts any guard)


def test_a_usb_serial_jtag_face_is_used_as_it_is(rig, tmp_path):
    from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
    img, manifest, tools, root = rig
    sysfs = _sysfs(tmp_path, "ttyACM9", "303a", "1001", "AA:BB:CC:DD:EE:01")
    port, why = common.esp_port(tools["devreg"], "board2", tools["esptool"], "esp32s3", sysfs=sysfs)
    assert (port, why) == ("/dev/ttyACM9", None)
    assert not os.path.exists(tools["esptool"] + ".args"), "esptool was not run"


def test_a_stock_app_whose_serial_is_not_the_mac_is_refused(rig, tmp_path):
    from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
    img, manifest, tools, root = rig
    sysfs = _sysfs(tmp_path, "ttyACM9", "303a", "0002", "oddball")
    port, why = common.esp_port(tools["devreg"], "board2", tools["esptool"], "esp32s3", sysfs=sysfs)
    assert port is None and "is not the MAC" in why


@pytest.mark.plugins
def test_facts_on_a_board_of_another_family_say_what_it_runs():
    """A Heltec V4 switched by snapshot to zephcore while its layout still declares zephyrtastic
    (and the other way round): `just facts` says what runs, as the plan does, not the declared
    family's state command timing out."""
    from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
    said = common.runs_instead(FakeBoard(), "zephcore")
    assert said.startswith("it runs zephyrtastic 0.4.40, not the zephcore its layout declares")
    assert "`just plan`" in said and "declare zephyrtastic" in said

    class Gone(FakeBoard):
        def exec(self, cmd, deadline=None, **_kw):
            raise broker_client.BrokerError("board9: `meshtastic state`: port closed")
    assert common.runs_instead(Gone(), "zephcore") is None, "nothing answers: nothing to add"


# ---- a board behind a USB-UART bridge: the chip's MAC, when the registry has it ------------

@pytest.mark.parametrize("on_record, chip_says, refused", [
    ("aa:bb:cc:11:22:33", "MAC: aa:bb:cc:11:22:33", None),
    ("AA:BB:CC:11:22:33", "MAC: aa:bb:cc:11:22:33", None),                 # case does not matter
    ("aa:bb:cc:11:22:33", "MAC: aa:bb:cc:00:11:22", "not aa:bb:cc:11:22:33"),
    ("aa:bb:cc:11:22:33", "A fatal error occurred", "cannot read the MAC"),
    ("", "MAC: aa:bb:cc:00:11:22", None)])                                 # no MAC on record
def test_a_bridge_port_is_the_board_only_when_its_chip_has_the_registrys_mac(
        rig, tmp_path, on_record, chip_says, refused):
    """cyd1 is matched by its CH340's VID:PID alone. With it unplugged and another CH340 board in
    its place, the ROM loader's MAC tells them apart before esptool writes."""
    from ansible_collections.rxrbench.core.plugins.module_utils.flash import common
    img, manifest, tools, root = rig
    sysfs = _sysfs(tmp_path, "ttyUSB1", "1a86", "7523", "")
    (root / "devreg.py").write_text(
        "import sys\n"
        "if sys.argv[1] == 'resolve': print('/dev/ttyUSB1')\n"
        f"if sys.argv[1] == 'policy': print({on_record!r}) if {on_record!r} else sys.exit(1)\n")
    (root / "esptool").write_text(f'#!/bin/sh\necho "$@" >> "$0.args"\necho "{chip_says}"\n')
    port, why = common.esp_port(tools["devreg"], "cyd1", tools["esptool"], "esp32", sysfs=sysfs)
    if refused:
        assert port is None and refused in why
    else:
        assert (port, why) == ("/dev/ttyUSB1", None)
    ran = open(tools["esptool"] + ".args").read() if os.path.exists(tools["esptool"] + ".args") else ""
    assert ("read-mac" in ran) is bool(on_record), "the chip is asked only when there is a MAC to compare"


def test_with_chip_blocks_on_the_host_the_rule_reads_the_chip_in_one_session(rig, monkeypatch):
    """The write rule's reads (table, otadata, app headers) are one chip_blocks `layout`, not
    one esptool connection per range (docs/ARCHITECTURE.md#checking-depth: measured 13 s on a CYD)."""
    import sys
    app_manager_table(rig[3], monkeypatch)
    helper = rig[3] / "chip_blocks.py"
    helper.write_text(
        "import importlib.util, json, os, sys\n"
        f"spec = importlib.util.spec_from_file_location('cb', {str(CHIP_BLOCKS)!r})\n"
        "cb = importlib.util.module_from_spec(spec); spec.loader.exec_module(cb)\n"
        "class Chip:\n"
        "    def __init__(self): self.b = open(os.environ['ESPTOOL_FLASH'], 'rb').read()\n"
        "    def read_flash(self, a, n): return self.b[a:a + n]\n"
        "open(sys.argv[0] + '.calls', 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'ok': True, 'size': 4 << 20, **cb.layout(Chip())}))\n")
    f = esp(rig, FakeBoard())
    f.blocks_tools = {"python": sys.executable, "chip_blocks": str(helper), "esptool": rig[2]["esptool"]}
    with pytest.raises(common.FlashError, match="covers test at 0x10000"):
        common.drive(f, rig[1])
    assert open(str(helper) + ".calls").read().split() == ["/dev/ttyACM9", "layout", "--at", "0x20000,0x220000"]
    calls = open(rig[2]["esptool"] + ".args").read()
    assert "read-flash" not in calls, "no range read on its own"


def test_naming_the_region_in_bench_overwrite_lets_the_write_take_it_and_only_it(rig, monkeypatch):
    app_manager_table(rig[3], monkeypatch)
    f = esp(rig, FakeBoard())
    f.overwrite = ("wifite",)                      # another region: not the one the write covers
    with pytest.raises(common.FlashError, match="covers test at 0x10000"):
        common.drive(f, rig[1])
    f = esp(rig, FakeBoard())
    f.overwrite = ("test",)
    assert common.drive(f, rig[1])["changed"]
