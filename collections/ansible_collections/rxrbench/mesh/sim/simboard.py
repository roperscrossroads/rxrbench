"""A simulated board of this plugin's firmware: core's sim board (core/sim/simboard.py) with
the plugin's factory image as the default (`just sim-build` stages it; sim/registry.yaml names
it; RXRBENCH_SIM_EXE overrides)."""
import os

from ansible_collections.rxrbench.core.sim import simboard as _core
from ansible_collections.rxrbench.core.sim.simboard import options_of, sha256_of  # noqa: F401  (re-exported)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, *[".."] * 5))
STORE = os.path.join(REPO, ".build", "store")


def _default_exe():
    """The image a sim board starts with: the first `sim_exe` the sim registry names, in the
    image store (`just sim-build` stages it), unless RXRBENCH_SIM_EXE says another."""
    if os.environ.get("RXRBENCH_SIM_EXE"):
        return os.environ["RXRBENCH_SIM_EXE"]
    try:
        import yaml  # pylint: disable=import-outside-toplevel
        with open(os.path.join(HERE, "registry.yaml")) as fh:
            devices = (yaml.safe_load(fh) or {}).get("devices") or {}
        exe = next(d["sim_exe"] for d in devices.values() if (d or {}).get("sim_exe"))
    except (OSError, StopIteration, ValueError):
        exe = ""
    return os.path.join(STORE, exe)


EXE = _default_exe()


def available():
    return os.access(EXE, os.X_OK)


class SimBoard(_core.SimBoard):
    def __init__(self, workdir, name="sim1", device_id=None, exe=None, seed=None):
        super().__init__(workdir, exe or EXE, name=name, device_id=device_id, seed=seed)
