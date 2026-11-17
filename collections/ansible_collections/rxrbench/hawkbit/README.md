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

```yaml
# a layout: one per device (in a *.sops.yaml file)
bench_hawkbit_token: "<32 characters, this device's alone>"
```

- `addr` is the target's controller id on the server. It is the registry's, and checked like
  any route's address before the server is told anything (`devreg guard --route hawkbit`).
- `host` is the bench host that speaks to the server for the device. It must reach the
  server's Management API, and it still reads the device over the device's `ssh` route.
- `bench_hawkbit_token` is the device's security token on the server. With it declared, the
  run enrolls the device: the target is made when the server has none, and its token set when
  the server holds another. Without it the target must already be there.
- Without the pin nothing changes: the device is written over ssh as before.

The device's own client must hold the same server, name and token. That is the device's
config, declared in its family's vocabulary beside the rest, from the same variable:

```yaml
# a Linux device: a file on its data partition (the client of device/ reads this one)
linux:
  files:
    /data/hawkbit/config:
      mode: "600"
      content: |
        SERVER=http://updates.example.org:8080
        CONTROLLER_ID={{ bench_routes.flash.addr }}
        TARGET_TOKEN={{ bench_hawkbit_token }}
```

```yaml
# a Zephyr device on the settings shell (the names are the application's; these are zapp's)
zephyr:
  digest_command: zapp digest
  settings:
    zhb/server: 192.0.2.10
    zhb/port: "8080"
    zhb/id: "{{ bench_routes.flash.addr }}"
    zhb/token: "{{ bench_hawkbit_token }}"
  secrets: {token: zhb/token}
```

The server's address here is where the device finds it, which need not be where the bench
host does (`bench_hawkbit.url`). A family with no config of its own (`zephyr_mcumgr`) has
nothing to declare this in: such a device's client is set up by whoever builds its firmware.

A Zephyr device under MCUboot takes its signed application the same way. It is read over MCUmgr
on the route it would otherwise be updated by, which a layout names when the device has several:

```yaml
reach:
  usb:     {host: bench1, can: [shell, diag]}
  eth:     {host: bench1, ip: 192.0.2.20, port: 1337, state: live, can: [flash]}
  hawkbit: {host: bench1, addr: node-a, state: live, can: [flash]}
```

```yaml
firmware: {family: zephyr_mcumgr, image: node-app-2.1.0}
routes: {flash: hawkbit, read: eth}
```

The firmware needs Zephyr's hawkBit client (`CONFIG_HAWKBIT`). The image is stored on the
server as a module of type `application`, the only kind that client accepts, under the staged
image's name. The device confirms the image itself, after whatever it calls healthy; the run
waits for that and never confirms it in the device's place. The zephyr plugin's example
application carries such a client (`just zapp-build <board> <version> --mcuboot --hawkbit`).

## What a run does

`just plan <layout>` reads the device and asks the server what it holds of the target. It
creates and assigns nothing, and says what an apply would do: enroll the device, write its
client's config, deliver its image. `just apply <layout>` first brings the two ends together,
whether or not the image differs:

- the server is made to hold the device with its declared token (`bench_hawkbit_token`);
- the device's config, its client's settings among it, is brought to its declared state
  before the image, because the device can fetch nothing until it knows the server.

Then, for a device that differs from its declared bundle, it:

1. refuses as a push would (a bundle for another `compatible`, a running slot not marked good),
   and also when the server has no such target and the layout declares no token to enroll it
   with, or when the server still has a delivery in flight for it;
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

The target must exist on the server with the token the device holds. A layout that declares
`bench_hawkbit_token` has the run see to both ends (above). By hand, the tool enrolls one:

```bash
echo '{"settings": {"url": "http://updates.example.org:8080", "user": "bench", "password": "..."},
       "target": "pi-a", "credential": "<the device token>"}' | ~/rxrbench/bin/hawkbit_route.py enroll
```

The login must be allowed to read and set target tokens. The server must accept them
(`authentication.targettoken.enabled`, a tenant setting).

## The tool

`host/bin/hawkbit_route.py` is deployed to a bench host with the other plugins' tools. It is
the whole of what knows the server: a client of hawkBit's Management API (as of hawkBit 1.1),
standard library only. It reads one JSON object on stdin and prints one; the login never
appears on a command line.

| Verb | What it does |
|---|---|
| `state` | what the server holds of a target: known or not, a delivery in flight or not, the last one's result, and with a token given, whether the target holds it. Changes nothing |
| `deliver` | uploads the image when the server does not have it, and assigns it. A version that already has another artifact is refused: on the server a version names one image |
| `enroll` | makes the target with the device's security token when the server has none, and sets the token when the target holds another |

## Limits

- **RAUC bundles for `linux_rauc` and `linux_slotguard`, and MCUboot images for a device
  written over MCUmgr.** Zephyr's own client reports a success for an update MCUboot swapped
  back (the previous image, confirmed, finds the saved action still open and closes it as
  done). A run here fails on the swap back all the same, and reports the disagreement.
- **A fault must reset the device.** An image that hangs on its test boot without a reset or a
  watchdog is never swapped back, and the delivery stays open until the wait runs out.
- **A version names one image, for good.** The server keeps every image it was given and
  locks a module once it has been assigned: it cannot be replaced or deleted. A bundle built
  again is another bundle, so it needs a version of its own, or the delivery is refused.
- **One device, one assignment.** hawkBit's rollouts (groups, thresholds) are not used: a run
  goes device by device and stops at the first failure, as for every route.
- **The device is still read by the bench host** (over ssh, or over MCUmgr). A device the
  bench host cannot reach at all cannot be verified, and is not a target of this route.
- **A device's client config can be declared only where its family has config.** A Linux
  device has its files and `zephyr_settings` its settings; `zephyr_mcumgr` has neither, and
  such a device's client is set up by its firmware or by hand.
- **The token is the only credential.** A gateway token, client certificates and TLS to the
  server are not handled here.
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
The test starts from nothing: the server has no target and the device's client no config, and
the first apply brings both.

`tests/sim/test_hawkbit_zephyr_sim.py` is the same for a Zephyr device (layout `zsimhawkbit`,
device `zh1`): core's emulated MCU under MCUboot, running the zephyr plugin's example
application with Zephyr's hawkBit client and a network (`just sim-zapp-qemu-build`). The
device's client settings are declared, the device fetches its image from the stand-in over the
emulated network, MCUboot runs it at the reset, the device confirms itself, and the bench reads
it over MCUmgr on its console. A second image is made to fail (it faults on its test boot):
the device resets, MCUboot returns to the image before it, and the apply fails on what the
device runs. Zephyr's own client then tells the server the update succeeded, so the run's
message ends "its hawkbit route's record says success: the record is wrong".

`tests/hw/test_hawkbit_route.py` is the route on real devices
([tests/hw/README.md](../../../../tests/hw/README.md)): against a real server, or against the
stand-in started on the bench host itself, and with an image made to fail.
