# The device's side: slotguard-hawkbit

A hawkBit client for a Linux device with RAUC A/B slots and
[slotguard](https://github.com/roperscrossroads/meta-slotguard)'s health gate. It tells the
server an update succeeded only after the new slot has booted and passed its gate, and that it
failed when the gate failed or the bootloader fell back. That is what makes the server's record
true, and what a staged rollout needs. The route works with any client (the run reads the
device itself); with one that reports before its reboot, the record is not worth reading.

POSIX sh; busybox is enough. It uses `curl` when the image has it, else `wget`. The header of
[`slotguard-hawkbit`](slotguard-hawkbit) says what each verb does, the files it keeps on
`/data/hawkbit` and its exit codes.

| File | What |
|---|---|
| `slotguard-hawkbit` | the client: `poll` (ask, install what is offered, reboot), `report` (after a boot, say how the pending update turned out), `status` |
| `slotguard-hawkbit.service`, `.timer` | `poll` on a timer |
| `slotguard-hawkbit-report.service` | `report` once per boot, ordered after the health gate, the target it gates and `rauc-mark-good.service` |

Its config is `/data/hawkbit/config` (mode 600, never in an image):

```sh
SERVER=http://updates.example.org:8080
CONTROLLER_ID=pi-a
TARGET_TOKEN=<the target's security token>
REBOOT_ON_UNHEALTHY=1
```

A slot that fails its gate is not left by the report alone: the bootloader returns to the old
slot only when the new one's boot attempts are used up, and each attempt is a boot.
`REBOOT_ON_UNHEALTHY=1` makes the client reboot the unhealthy slot until that happens.

`tests/sim/test_hawkbit_sim.py` runs it in the simulated A/B machine, with the test standing in
for the timer.
