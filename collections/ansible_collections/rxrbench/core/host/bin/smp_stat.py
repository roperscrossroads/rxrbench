#!/usr/bin/env python3
"""Read the SMP OS-management diagnostics smpmgr's CLI does not expose.

A firmware may enable MCUMGR_GRP_OS_TASKSTAT (per-thread name/state/stack/runtime) and
MCUMGR_GRP_OS_INFO, but `smpmgr`'s `os` group implements only `echo` and `reset`. The text
route fails too: `smpmgr shell "kernel thread list"` returns MGMT_ERR.EMSGSIZE once the reply
exceeds CONFIG_SHELL_BACKEND_DUMMY_BUF_SIZE (2048 by default). smp.os_management defines
TaskStatisticsReadRequest/Response; this is the missing CLI verb, on the same smpclient the
rest of the tool uses. taskstat is a structured request, so the shell buffer does not limit it.

Usage (on a host with a Bluetooth adapter, under the venv that has smpclient, which the
default install puts at ~/.venv-mcumgr):
  smp_stat.py <ble-addr> [taskstat|osinfo|mempool|all]

  e.g. smp_stat.py 02:00:00:00:00:01 taskstat
"""
import asyncio
import sys

from smpclient import SMPClient
from smpclient.transport.ble import SMPBLETransport
# The wrappers come from smpclient.requests.*, not from smp.*. The
# smp.* classes are the wire messages and carry no _Response binding, so
# SMPClient.request() raises AttributeError on them. smpmgr's own commands import
# from smpclient.requests.os_management for exactly this reason.
from smpclient.requests.os_management import (
    MemoryPoolStatisticsRead,
    OSApplicationInfoRead,
    TaskStatisticsRead,
)

TIMEOUT_S = 30.0


def _fmt_taskstat(rep) -> None:
    tasks = getattr(rep, "tasks", None)
    if not tasks:
        print(f"  (no tasks field; raw: {rep})")
        return
    # Zephyr reports a subset; print whatever fields each entry actually has.
    rows = []
    for name, t in tasks.items():
        d = t.model_dump() if hasattr(t, "model_dump") else dict(t)
        rows.append((name, d))
    cols = ["prio", "tid", "state", "stkuse", "stksiz", "cswcnt", "runtime"]
    print(f"  {'thread':<24} " + " ".join(f"{c:>8}" for c in cols))
    print(f"  {'-'*24} " + " ".join("-" * 8 for _ in cols))
    for name, d in sorted(rows, key=lambda r: -(r[1].get("stkuse") or 0)):
        cells = " ".join(f"{str(d.get(c, '-')):>8}" for c in cols)
        print(f"  {name:<24} {cells}")
    # Stack headroom is the number that matters in a post-mortem.
    worst = [
        (n, d) for n, d in rows
        if d.get("stkuse") is not None and d.get("stksiz")
    ]
    if worst:
        n, d = max(worst, key=lambda r: r[1]["stkuse"] / max(r[1]["stksiz"], 1))
        pct = 100.0 * d["stkuse"] / max(d["stksiz"], 1)
        print(f"\n  deepest stack: {n} at {d['stkuse']}/{d['stksiz']} ({pct:.1f}%)")


async def run(addr: str, what: str) -> int:
    transport = SMPBLETransport()
    client = SMPClient(transport, addr, timeout_s=TIMEOUT_S)
    await client.connect()
    try:
        wanted = ("taskstat", "osinfo", "mempool") if what == "all" else (what,)
        for w in wanted:
            if w == "taskstat":
                print("== taskstat ==")
                _fmt_taskstat(await client.request(TaskStatisticsRead()))
            elif w == "osinfo":
                print("== os info ==")
                rep = await client.request(OSApplicationInfoRead(format="sabmnpirv"))
                print(f"  {getattr(rep, 'output', rep)}")
            elif w == "mempool":
                print("== memory pools ==")
                rep = await client.request(MemoryPoolStatisticsRead())
                print(f"  {rep}")
            else:
                print(f"unknown: {w}", file=sys.stderr)
                return 2
            print()
    finally:
        await client.disconnect()
    return 0


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    addr = sys.argv[1]
    what = sys.argv[2] if len(sys.argv) > 2 else "taskstat"
    sys.exit(asyncio.run(run(addr, what)))


if __name__ == "__main__":
    main()
