# rxrbench.hawkbit

An [Eclipse hawkBit](https://eclipse.dev/hawkbit/) update server as a **route** to a device.
The plugin brings no family: a family says what a device runs and how that is read, and this
plugin adds one more way an image can get there. The contract it fills in:
[docs/PLUGINS.md, "A route that delivers"](../../../../docs/PLUGINS.md#a-route-that-delivers).

With every other route the bench host writes the device. With this one it does not. The bench
host tells the server which image the device should have; the device's own update client asks
the server, fetches the image, installs it and reboots, on its own schedule. A device behind
NAT, or on a network the bench host cannot write across, can be updated this way as long as
it can reach the server.

What does not change is who says the update worked. The device's family still reads it before
the delivery and after it, over the family's own route, and that answer decides. The server's
record is reported beside it.

## A device's declaration

```yaml
# the registry: the device's routes
reach:
  ssh:     {host: bench1, ip: 192.0.2.10, user: root, state: live, can: [flash, shell, diag]}
  hawkbit: {host: bench1, addr: pi-a, state: live, can: [flash]}
```

```yaml
# a layout: host_vars/<device>.yml
firmware: {family: linux_slotguard, image: node-image}
routes: {flash: hawkbit}            # this device takes its bundles through the server
bench_wait_good: 420                # as for a push: wait for the slot's health gate
```

```yaml
# a layout: group_vars (the login in a *.sops.yaml file)
bench_hawkbit:
  url: http://updates.example.org:8080
  user: bench
  password: "..."
bench_delivery_timeout: 1800        # seconds a device has to take a delivery and reboot
```

- `addr` is the target's controller id on the server. It is the registry's, and checked like
  any route's address before the server is told anything (`devreg guard --route hawkbit`).
- `host` is the bench host that speaks to the server for the device. It must reach the
  server's Management API, and it still reads the device over the device's `ssh` route.
- Without the pin nothing changes: the device is written over ssh as before.

## What a run does

`just plan <layout>` reads the device over ssh and asks the server what it holds of the target.
It creates and assigns nothing. `just apply <layout>`, for a device that differs from its
declared bundle:

1. refuses as a push would (a bundle for another `compatible`, a running slot not marked good),
   and also when the server has no such target or still has a delivery in flight for it;
2. makes the bundle a software module and a distribution set when the server has neither (found
   by name, version and type, so an image is uploaded once), and assigns the set to the target;
3. waits until the device has rebooted (`bench_delivery_timeout`). A delivery the device
   reports as failed before any reboot ends the wait with the device's own words;
4. reads the slot the device runs and, with `bench_wait_good`, waits for its health gate. A
   device back on its old slot fails the run, whatever the server was told.

The result carries `delivery`: the route's record after the run, and `agrees`, whether it
matches what the device runs. `agrees: false` is a server record that is wrong.

## What the device needs

A hawkBit client configured with the server and the target's token. Which client matters for
what the server's record is worth:

- A client that reports `closed/success` as soon as the slot is written, before the reboot
  (`rauc-hawkbit-updater` does), leaves the server saying success for an update that may yet
  roll back. A run here still fails on the rollback, and reports the disagreement.
- A client that reports `proceeding` at install and closes the action only after the next boot
  (success once the slot is marked good, failure when its gate failed or the bootloader fell
  back) makes the server's record true, which is what a staged rollout needs.

[`device/`](device/README.md) holds such a client for a device with RAUC and slotguard's health
gate, `slotguard-hawkbit`, with the units that run it.

The target must exist on the server with the token the device holds. The tool creates one:

```bash
echo '{"settings": {"url": "http://updates.example.org:8080", "user": "bench", "password": "..."},
       "target": "pi-a", "token": "<the device token>"}' | ~/rxrbench/bin/hawkbit_route.py target
```

## The tool

`host/bin/hawkbit_route.py` is deployed to a bench host with the other plugins' tools. It is
the whole of what knows the server: a client of hawkBit's Management API (as of hawkBit 1.1),
standard library only. It reads one JSON object on stdin and prints one; the login never
appears on a command line.

| Verb | What it does |
|---|---|
| `state` | what the server holds of a target: known or not, a delivery in flight or not, the last one's result. Changes nothing |
| `deliver` | uploads the image when the server does not have it, and assigns it. A version that already has another artifact is refused: on the server a version names one image |
| `target` | creates a target with a security token when there is none; an existing one is left as it is |

## Limits

- **RAUC bundles, for `linux_rauc` and `linux_slotguard`.** An MCUboot image for a Zephyr
  device is not carried yet (hawkBit's Zephyr client takes only a module of type
  `application`; the tool knows the mapping, no family takes the route).
- **A version names one image, for good.** The server keeps every image it was given and
  locks a module once it has been assigned: it cannot be replaced or deleted. A bundle built
  again is another bundle, so it needs a version of its own, or the delivery is refused.
- **One device, one assignment.** hawkBit's rollouts (groups, thresholds) are not used: a run
  goes device by device and stops at the first failure, as for every route.
- **The device is still read over ssh.** A device the bench host cannot reach at all cannot be
  verified, and is not a target of this route.
- Nothing here installs, backs up or secures the server.

## Tests

`sim/fakeserver.py` is a stand-in server with the states and JSON shapes of hawkBit 1.1, and
faults to inject. `tests/unit/test_hawkbit_route.py` holds the tool to it;
`tests/unit/test_route_delivery.py` drives the RAUC flasher through the route with a scripted
device: a delivery that holds, one that rolls back, one the server was told had succeeded when
it had not, and a plan that writes nothing.

`tests/sim/test_hawkbit_sim.py` is the whole of it on a simulated device
([docs/SIMULATED.md](../../../../docs/SIMULATED.md), layout `simabhawkbit`): the Linux plugin's
QEMU A/B machine as a second device, `hb1`, running `slotguard-hawkbit`, with `apply.yml` on a
layout that pins the route. A good bundle is fetched, booted and gated, and the server's
record is true; a bundle that fails its gate fails the run, is reported to the server by the
device, and is rolled back. `RXRBENCH_TEST_HAWKBIT` names a real server to run it against.
