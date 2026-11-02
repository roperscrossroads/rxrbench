#!/usr/bin/env python3
"""Survey probe: what stock Meshtastic says of itself over its own phone API, read-only.

Usage: probe_meshtastic.py <port>. Prints one JSON object. Run by rxrbench's survey on a bench
host (usb_survey), in a child process so a hung library can be killed; needs the `meshtastic`
library in the interpreter it is run with.
"""
import json, sys
from meshtastic.serial_interface import SerialInterface
i = SerialInterface(devPath=sys.argv[1], noNodes=True)
try:
    m, md = i.myInfo, i.metadata
    lora = i.localNode.localConfig.lora
    from meshtastic.protobuf import config_pb2, mesh_pb2
    out = {
        "node_id": "0x%08x" % m.my_node_num if m else None,
        "firmware_version": md.firmware_version if md else None,
        "hw_model": mesh_pb2.HardwareModel.Name(md.hw_model) if md else None,
        "has_bluetooth": bool(md.hasBluetooth) if md else None,
        "has_wifi": bool(md.hasWifi) if md else None,
        "region": config_pb2.Config.LoRaConfig.RegionCode.Name(lora.region),
        "modem_preset": config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset),
        "tx_enabled": bool(lora.tx_enabled),
    }
    u = i.getMyUser() or {}
    mac = u.get("macaddr")
    if mac:
        import base64
        try:
            mac = ":".join("%02X" % b for b in base64.b64decode(mac))
        except ValueError:
            pass
    out.update({"long_name": u.get("longName"), "short_name": u.get("shortName"),
                "ble_mac": mac})
    print(json.dumps(out))
finally:
    i.close()
