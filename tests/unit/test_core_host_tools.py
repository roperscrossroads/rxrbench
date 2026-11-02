"""Every tool in core's host/bin/ reaches a bench host: the bench_host role lists core's one by
one (a plugin's are globbed), so a new one left off the list runs nowhere. swd_survey.py
was, until this test."""
import glob
import os
import re

CORE = os.path.join(os.path.dirname(__file__), "..", "..", "collections", "ansible_collections",
                    "rxrbench", "core")


def test_every_core_host_tool_is_deployed():
    tools = {os.path.basename(p) for p in glob.glob(os.path.join(CORE, "host", "bin", "*.py"))}
    listed = open(os.path.join(CORE, "roles", "bench_host", "defaults", "main.yml")).read()
    deployed = set(re.findall(r"host/bin/([\w.]+\.py)\"?, dest: rxrbench/bin/\1", listed))
    assert tools and tools <= deployed, f"not deployed to bench hosts: {sorted(tools - deployed)}"
