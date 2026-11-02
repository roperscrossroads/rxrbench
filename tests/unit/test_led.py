"""The bench LED's answer, read; and the label sheet's serial tails."""
import importlib.util
import os

from ansible_collections.rxrbench.mesh.plugins.modules import zephyrtastic_led

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_the_led_is_read_with_and_without_colours():
    assert zephyrtastic_led.parse("mode: in-use  color: cyan  (RGB: ...)") == \
        {"mode": "in-use", "color": "cyan"}
    assert zephyrtastic_led.parse("mode: idle  (RGB: idle red, in-use green, locate blue)") == \
        {"mode": "idle", "color": None}, "firmware before colours"
    assert zephyrtastic_led.parse("meshtastic - Meshtastic mesh radio commands.\nSubcommands:") \
        is None, "firmware without the LED"


def test_a_label_shows_the_tail_of_what_the_board_is_recognised_by():
    spec = importlib.util.spec_from_file_location("labels", os.path.join(REPO, "tools", "labels.py"))
    labels = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(labels)
    assert labels.recognised_by({"match": {"usb_serial": "0123456789ABCDEF"}}) == "ABCDEF"
    assert labels.recognised_by({"match": {
        "serial_by_id": "usb-Espressif_USB_JTAG_serial_debug_unit_AA:BB:CC:12:34:56-if00"}}) \
        == "123456"
    assert labels.recognised_by({}) is None
