#!/usr/bin/env python3
"""simbench.py --registry FILE --run-dir DIR [--store DIR] [--broker FILE] [--dialects DIRS] BOARD...

A bench of simulated boards.

For each board: the sim board process (a firmware built for native_sim, the registry entry's
`sim_exe` in the image store) and the same serial broker a bench host runs, on DIR/<board>.sock.
The tool then reaches a simulated board the way it reaches a real one. Runs until SIGTERM or
SIGINT; each board's config stays in DIR/<board>.flash, so the next run is a reboot.

The registry is the simulated bench's own, in a site registry's shape with no hardware in it
(the example bench's, site.example/sim/registry.yaml; a plugin's sim brings another). Runs from the source tree, or on a
bench host where the sim_bench role put it beside simboard.py (then --broker names the broker
the serial_broker role installed and --dialects its console rules).
"""
import argparse
import importlib.util
import os
import signal
import sys
import threading

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, *[".."] * 5))
IN_TREE = os.path.isdir(os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core"))
if IN_TREE:
    sys.path.insert(0, os.path.join(REPO, "collections"))
    from ansible_collections.rxrbench.core.sim import simboard  # pylint: disable=wrong-import-position
    # The broker runs from the source tree here: it is told where the loaded plugins' console
    # rules are, as the role puts them beside it on a bench host.
    from ansible_collections.rxrbench.core.plugins.module_utils import site  # pylint: disable=wrong-import-position
    from ansible_collections.rxrbench.core.plugins.plugin_utils import registry  # pylint: disable=wrong-import-position
    os.environ.setdefault("RXRBENCH_DIALECTS", os.pathsep.join(
        os.path.join(d, "host", "dialects.d") for d in registry.dirs(site.plugins()).values()))
    BROKER = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "broker.py")
    STORE = os.path.join(REPO, ".build", "store")
else:   # on a bench host, beside simboard.py, the broker where the serial_broker role put it
    sys.path.insert(0, HERE)
    import simboard  # pylint: disable=wrong-import-position,import-error
    BROKER = os.path.expanduser("~/rxrbench/lib/broker.py")
    STORE = os.path.join(HERE, "store")


def load_broker(path):
    spec = importlib.util.spec_from_file_location("rxrbench_broker", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def factory_image(dev, store):
    """The image a sim board starts with: its registry entry's `sim_exe`, relative to the
    image store unless absolute."""
    exe = (dev or {}).get("sim_exe") or ""
    return exe if os.path.isabs(exe) else os.path.join(store, exe)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--registry", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--store", default=STORE)
    ap.add_argument("--broker", default=BROKER, help="the serial broker (broker.py)")
    ap.add_argument("--dialects", help="console rule directories for the broker (RXRBENCH_DIALECTS)")
    ap.add_argument("boards", nargs="+")
    a = ap.parse_args(argv)
    if a.dialects:
        os.environ["RXRBENCH_DIALECTS"] = a.dialects
    mb = load_broker(a.broker)
    with open(a.registry) as f:
        devices = yaml.safe_load(f)["devices"]
    for name in a.boards:
        if name not in devices:
            sys.exit(f"{name}: not in {a.registry}")
        exe = factory_image(devices[name], a.store)
        # an emulated MCU's "executable" is its image's manifest (simboard.py, console qemu)
        if not (os.access(exe, os.X_OK) or (devices[name].get("sim_console") == "qemu" and os.path.isfile(exe))):
            sys.exit(f"{name}: its sim image is not built ({exe})")
    os.makedirs(a.run_dir, mode=0o700, exist_ok=True)

    rigs = []
    for name in a.boards:
        dev = devices[name]
        sim = simboard.SimBoard(a.run_dir, factory_image(dev, a.store), name=name,
                                device_id=(dev.get("match") or {}).get("sim_device_id"),
                                seed=dev.get("sim_seed"), console=dev.get("sim_console") or "pty").start()
        console = mb.Console(name, mb.path_opener(sim.path),
                             os.path.join(a.run_dir, f"{name}.log"))
        broker = mb.Broker(console, os.path.join(a.run_dir, f"{name}.sock"))
        thread = threading.Thread(target=broker.run, name=name, daemon=True)
        thread.start()
        rigs.append((sim, broker, thread))
        print(f"{name}: pid {sim.proc.pid}, socket {broker.sock_path}", flush=True)

    done = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: done.set())
    with open(os.path.join(a.run_dir, "simbench.pid"), "w") as f:
        f.write(str(os.getpid()))
    print("up", flush=True)
    done.wait()
    for sim, broker, thread in rigs:
        broker.shutdown()
        thread.join(timeout=2)
        sim.stop()
    os.unlink(os.path.join(a.run_dir, "simbench.pid"))


if __name__ == "__main__":
    main()
