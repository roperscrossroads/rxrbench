import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The collections the tests import: the repository's, or another tree that holds fewer of them
# (`tools/core_only.py`: core and a fixture plugin alone, core-portability P1).
sys.path.insert(0, os.environ.get("RXRBENCH_COLLECTIONS") or os.path.join(REPO, "collections"))
# A tool a test loads by its file finds its sibling modules (tools/collections_path.py).
sys.path.insert(0, os.path.join(REPO, "tools"))

import pytest  # noqa: E402

from ansible_collections.rxrbench.core.plugins.module_utils import familyspec, site  # noqa: E402
from ansible_collections.rxrbench.core.plugins.plugin_utils import families

# The site's settings (.env) before any test module resolves a path: under `just` they are
# already in the environment, under bare `pytest` they are not, and the real-layout tests would
# otherwise look for the registry at the default path and quietly skip.
site.load_env()

#: What in a site's .env reaches a live service, and must not from a test run: the plug relay's
#: notices and its post to a coordination bus, a facts commit, and the Ansible callbacks a site
#: enables for its runs (one that publishes each run to a broker would publish every test play,
#: its boards as devices). Set, not removed: every later load of .env (a tool a test imports, a
#: child process, `just`) keeps what is already set. A test about one of them sets it itself.
def _repo_callbacks():
    """The callbacks the repository's ansible.cfg enables: what a run has without a site's."""
    import configparser  # pylint: disable=import-outside-toplevel
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(os.path.join(REPO, "ansible.cfg"))
    return cfg.get("defaults", "callbacks_enabled", fallback="").strip()


# (an empty callback path would make ansible search its working directory: an empty directory)
LIVE_SETTINGS = {"RXRBENCH_BENCH_NTFY": "", "RXRBENCH_BUS_SEND": "", "RXRBENCH_FACTS_COMMIT": "",
                 "ANSIBLE_CALLBACK_PLUGINS": __import__("tempfile").mkdtemp(prefix="rxrbench-no-callbacks-"),
                 "ANSIBLE_CALLBACKS_ENABLED": _repo_callbacks()}
os.environ.update(LIVE_SETTINGS)

# A broker run from the source tree (the tests load host/broker/broker.py) has no dialects.d
# beside it: every loaded plugin's console rules are named here, as the role deploys them on
# a host (a plugin left out would have its secrets reach a test bench's log).
from ansible_collections.rxrbench.core.plugins.plugin_utils import registry  # noqa: E402

os.environ.setdefault("RXRBENCH_DIALECTS", os.pathsep.join(
    os.path.join(d, "host", "dialects.d") for d in registry.dirs(site.plugins()).values()))


@pytest.fixture(autouse=True)
def no_real_location_index(tmp_path_factory, monkeypatch):
    """Layouts resolve a board's host from the plug relay's state on this machine
    (module_utils/location.py). Tests must not see the real bench: an empty index."""
    monkeypatch.setenv("RXRBENCH_LOCATION_DIR", str(tmp_path_factory.mktemp("no-location")))
    # nor this machine's own USB devices (a bench host that is the workstation is read live),
    # nor its bench-host state (a flash marks its board busy there)
    monkeypatch.setenv("RXRBENCH_SYSFS_USB", str(tmp_path_factory.mktemp("no-usb")))
    monkeypatch.setenv("RXRBENCH_HOST_STATE", str(tmp_path_factory.mktemp("no-host-state")))


@pytest.fixture(autouse=True)
def the_families_table():
    """Code that runs on a bench host is handed the families as data (familyspec.py); a test
    that calls it directly gets the table every loaded family resolves to."""
    familyspec.load(families.specs())
    yield
    familyspec.load({})


#: The fixture plugin's collections tree (fixture.widgets: core-portability P1, P6).
FIXTURE_COLLECTIONS = os.path.join(REPO, "tests", "fixtures", "collections")


@pytest.fixture
def widgets():
    """The fixture plugin loaded beside the site's: a test of core whose data is a family uses
    fixture.widgets' families (`widget`, `probechip`), so it runs with core alone as well
    (`just core-only`) instead of being marked `plugins`."""
    names = [n for n in site.plugins() if n != "fixture.widgets"] + ["fixture.widgets"]
    families.load(names, roots=[FIXTURE_COLLECTIONS])
    familyspec.load(families.specs())
    yield families
    families.load()
    familyspec.load(families.specs())


@pytest.fixture(autouse=True)
def no_admin_pacing(monkeypatch):
    """The Meshtastic writer leaves a gap after each admin message (mesh config.PACE) for real
    firmware's queue; against fakes it would only slow the tests. A test about the gap sets it.
    Core runs without the mesh plugin (core-portability P1): then there is nothing to pace."""
    try:
        from ansible_collections.rxrbench.mesh.plugins.module_utils import config  # pylint: disable=import-outside-toplevel
    except ModuleNotFoundError:
        return
    monkeypatch.setattr(config, "PACE", 0)

BUNDLED = ("mesh", "linux", "zephyr", "nuttx", "launcher")
_IMPORTS_BUNDLED = re.compile(r"""^(?:from|import) ansible_collections\.rxrbench\.(?:%s)\b"""
                              r"""|import_module\(\s*["']ansible_collections\.rxrbench\.(?:%s)\b"""
                              % ("|".join(BUNDLED), "|".join(BUNDLED)), re.M)


def pytest_configure(config):
    config.addinivalue_line("markers", "plugins: a test of core whose data is a bundled plugin's "
                            "family, board type or dialect; tools/core_only.py deselects it")


def pytest_ignore_collect(collection_path, config):
    """Core alone (tools/core_only.py): a test file that imports a bundled plugin is that
    plugin's test, not core's."""
    if not os.environ.get("RXRBENCH_CORE_ONLY") or collection_path.suffix != ".py":
        return None
    return True if _needs_bundled(collection_path) else None


_SUPPORT = re.compile(r"^from tests\.support\.(\w+) import|^import tests\.support\.(\w+)", re.M)


def _needs_bundled(path, seen=None):
    """Whether a test file, or a tests/support module it imports, imports a bundled plugin."""
    seen = set() if seen is None else seen
    if path in seen:
        return False
    seen.add(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if _IMPORTS_BUNDLED.search(text):
        return True
    support = path.parent.parent / "support" if path.parent.name != "support" else path.parent
    return any(_needs_bundled(support / f"{a or b}.py", seen) for a, b in _SUPPORT.findall(text))
