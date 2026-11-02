"""tools/stage_raw.py --flash-map: an image's partitions from the build's own devicetree."""
import importlib.util
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("stage_raw", os.path.join(REPO, "tools", "stage_raw.py"))
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)

DTS = """
    flash@10000000 {
        partitions {
            code_partition: partition@100 {
                label = "code";
                reg = < 0x100 0x7dff00 >;             /* in board.dts:54 */
            };
            storage_partition: partition@7e0000 {
                compatible = "zephyr,mapped-partition";
                label = "storage";
                reg = < 0x7e0000 0x20000 >;           /* in kb.overlay:11 */
            };
        };
    };
"""


def test_every_labelled_partition_is_mapped_by_its_devicetree_reg(tmp_path):
    (tmp_path / "zephyr").mkdir()
    (tmp_path / "zephyr" / "zephyr.dts").write_text(DTS)
    assert sr.flash_map(str(tmp_path)) == {"CODE": "0x100", "CODE_SIZE": "0x7dff00",
                                           "STORAGE": "0x7e0000", "STORAGE_SIZE": "0x20000"}


def test_a_devicetree_with_no_partitions_is_refused(tmp_path):
    (tmp_path / "zephyr").mkdir()
    (tmp_path / "zephyr" / "zephyr.dts").write_text("/ { };")
    with pytest.raises(SystemExit, match="names no partitions"):
        sr.flash_map(str(tmp_path))


USB = """
    chosen {
        zephyr,console = &snippet_cdc_acm_console_uart;
        zephyr,shell-uart = &snippet_cdc_acm_console_uart;
    };
    usbd: usbd@50110000 {
        snippet_cdc_acm_console_uart: snippet_cdc_acm_console_uart {
            compatible = "zephyr,cdc-acm-uart";
        };
    };
"""
UART = """
    chosen {
        zephyr,console = &uart0;
        zephyr,shell-uart = &uart0;
    };
    uart0: uart@40070000 {
        compatible = "raspberrypi,pico-uart";
    };
"""


@pytest.mark.parametrize("dts, where", [(USB, "usb"), (UART, "uart"), ("chosen { };", "none"),
                                        ("/ { };", None)])
def test_where_a_builds_shell_answers_is_read_from_its_devicetree(tmp_path, dts, where):
    """A build of zapp without its USB console snippet (shell on uart0) says so in its manifest,
    so it is not written to a board the bench reaches by USB."""
    (tmp_path / "zephyr").mkdir()
    (tmp_path / "zephyr" / "zephyr.dts").write_text(dts)
    assert sr.console_of(str(tmp_path)) == where
