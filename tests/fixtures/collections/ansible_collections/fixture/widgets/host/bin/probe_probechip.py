#!/usr/bin/env python3
"""The fixture plugin's survey tool for its probe-flashed family: reads the chips wired to a
debug probe. Here it reads nothing real; it says what it was given, so a test sees the
survey's contract (`--probe <registry name> --wired <json>`)."""
import argparse
import json

ap = argparse.ArgumentParser()
ap.add_argument("--probe", required=True)
ap.add_argument("--wired", required=True)
a = ap.parse_args()
wired = json.loads(a.wired)
print(json.dumps({"probe": a.probe, "chips": [{"name": w["name"], "uid": w["uid"]} for w in wired]}))
