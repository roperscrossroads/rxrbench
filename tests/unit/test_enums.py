"""The enum tables must match the firmware's generated protobuf header, when one is here."""
import glob
import os
import re

import pytest

from ansible_collections.rxrbench.mesh.plugins.module_utils import enums


def _workspace():
    """The zephyrtastic workspace on this machine (catalog `sources:`), or a path that is not."""
    from ansible_collections.rxrbench.core.plugins.module_utils import site
    try:
        return site.firmware_source("zephyrtastic")["workspace"]
    except KeyError:
        return "/nonexistent"


HEADERS = sorted(glob.glob(os.path.join(
    _workspace(),
    "build-*/meshtastic/modules/meshtastic-zephyr/src/proto/meshtastic/config.pb.h")),
    key=os.path.getmtime)


def header_enum(text, name):
    body = re.search(r"typedef enum _%s \{(.*?)\} %s;" % (name, name), text, re.S).group(1)
    return {int(v): k for k, v in re.findall(r"%s_([A-Z0-9_]+) = (\d+)" % name, body)}


@pytest.mark.skipif(not HEADERS, reason="no firmware build tree on this machine")
@pytest.mark.parametrize("table,name", [
    (enums.MODEM_PRESET, "meshtastic_Config_LoRaConfig_ModemPreset"),
    (enums.REGION, "meshtastic_Config_LoRaConfig_RegionCode"),
    (enums.DEVICE_ROLE, "meshtastic_Config_DeviceConfig_Role"),
    (enums.REBROADCAST_MODE, "meshtastic_Config_DeviceConfig_RebroadcastMode"),
])
def test_table_matches_the_newest_firmware_header(table, name):
    assert table == header_enum(open(HEADERS[-1]).read(), name)


def test_the_display_names_cover_every_preset_but_one():
    # VERY_LONG_SLOW has no display name in the firmware's table (it is not selectable there).
    from ansible_collections.rxrbench.mesh.plugins.module_utils import state
    assert set(enums.MODEM_PRESET.values()) - set(state.PRESETS.values()) == {"VERY_LONG_SLOW"}
