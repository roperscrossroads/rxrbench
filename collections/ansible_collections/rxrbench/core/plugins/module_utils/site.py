"""Where a site's things are: one place, so nothing else hard-codes a path.

rxrbench is the tool; a **site** is one bench's own configuration: its layouts, its device
registry and survey facts, its bench hosts. The site lives in `site/` (gitignored, best kept as
its own private git repo); `site.example/` in the tool shows the shape and carries the
simulated bench the tests use. Settings come from the environment, which `just` fills from
`.env` (gitignored; `.env.example` lists every setting); tools started outside `just` (the
relay's systemd unit) call `load_env()`.

Settings, all optional (`.env.example` lists every one with its default and what reads it):
RXRBENCH_ROOT (the tool's root: where `.env`, `site.example/` and a relative site are; default
the checkout this collection sits in; set it in the environment, since `.env` is found through
it), RXRBENCH_SITE (the site directory, default `site`, relative to the repo or absolute),
DEVREG_REGISTRY (default `<site>/registry.yaml`; devreg reads the same name), RXRBENCH_FACTS
(default `<site>/facts`). The relay's, the workstation's, the simulated bench's and the
debug rig's settings are read where they are used.
"""
import os

# The tool's root: RXRBENCH_ROOT, else the checkout six levels above this file, found through
# any link to the collection (a tree that holds core alone links to it). A core installed apart
# from the checkout (by ansible-galaxy) needs RXRBENCH_ROOT (docs/ARCHITECTURE.md#core-portability).
REPO = os.path.abspath(os.path.expanduser(os.environ.get("RXRBENCH_ROOT") or os.path.join(
    os.path.dirname(os.path.realpath(__file__)), *[".."] * 6)))
EXAMPLE = os.path.join(REPO, "site.example")
#: This collection: its roles, its playbooks, its catalog and what it deploys to a bench host.
CORE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CATALOG = os.path.join(CORE, "catalog")
HOST = os.path.join(CORE, "host")
PLAYBOOKS = os.path.join(CORE, "playbooks")


def load_env(path=None):
    """KEY=VALUE lines from the repo's .env into os.environ, without overriding what is set.
    Read as `just` reads it (dotenv-load): blank lines and # comments are skipped, a quoted
    value is what is inside its quotes, and an unquoted value ends at a ` #` comment."""
    path = path or os.path.join(REPO, ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), _env_value(v.strip()))


def _env_value(v):
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        return v[1:end] if end > 0 else v[1:]
    for i, ch in enumerate(v):
        if ch == "#" and (i == 0 or v[i - 1] in " \t"):
            return v[:i].rstrip()
    return v


def _path(value):
    value = os.path.expanduser(value)
    return value if os.path.isabs(value) else os.path.join(REPO, value)


def site_dir():
    return _path(os.environ.get("RXRBENCH_SITE") or "site")


def registry_path():
    return _path(os.environ.get("DEVREG_REGISTRY") or os.path.join(site_dir(), "registry.yaml"))


def facts_dir():
    return _path(os.environ.get("RXRBENCH_FACTS") or os.path.join(site_dir(), "facts"))


def rf_rules_path():
    """The site's RF ceiling (rfrules.ceiling): `<site>/rf_rules.yml`; absent, the built-in
    defaults are the ceiling."""
    return os.path.join(site_dir(), "rf_rules.yml")


def plugins(root=None):
    """The plugin collections loaded (`namespace.collection`, in order): RXRBENCH_PLUGINS
    (comma-separated) when it is set, else `plugins:` in `<root>/plugins.yml`, where `root` is
    the site; a site without the file loads what the examples do. A `root` given is read
    alone: no file there, no plugins."""
    env = os.environ.get("RXRBENCH_PLUGINS")
    if env is not None:
        return [p.strip() for p in env.split(",") if p.strip()]
    path = os.path.join(root or site_dir(), "plugins.yml")
    if not os.path.exists(path) and root is None:
        path = os.path.join(EXAMPLE, "plugins.yml")
    if not os.path.exists(path):
        return []
    import yaml  # pylint: disable=import-outside-toplevel
    with open(path) as fh:
        return list((yaml.safe_load(fh) or {}).get("plugins") or [])


def layout_dirs():
    """Where layouts are looked for: the site's, then the examples (the simulated bench)."""
    return [os.path.join(site_dir(), "layouts"), os.path.join(EXAMPLE, "layouts")]


def layout_path(name):
    for d in layout_dirs():
        p = os.path.join(d, name)
        if os.path.isdir(p):
            return p
    raise FileNotFoundError(f"no layout {name!r} in " + " or ".join(layout_dirs()))


def firmware_source(family):
    """Where a firmware family's source lives on this machine, from the catalogs' `sources:`
    (the site's over the tool's): {"tree": <its repository>, "workspace": <the west workspace
    it builds in>}. The two differ in shape between families, so both are stated."""
    import yaml  # pylint: disable=import-outside-toplevel
    found = {}
    for d in (CATALOG, os.path.join(EXAMPLE, "catalog"),
              os.path.join(site_dir(), "catalog")):
        p = os.path.join(d, "firmware.yml")
        if os.path.exists(p):
            with open(p) as fh:
                found.update(((yaml.safe_load(fh) or {}).get("sources") or {}).get(family) or {})
    if not found:
        raise KeyError(f"no source for firmware family {family!r}: add it under `sources:` in "
                       f"{os.path.join(site_dir(), 'catalog', 'firmware.yml')}")
    return {k: os.path.expanduser(str(v)) for k, v in found.items()}


def _bench_hosts_group():
    """The `bench_hosts` group of the site's `hosts` layout (hosts.yml), read once per call."""
    import yaml  # pylint: disable=import-outside-toplevel
    with open(os.path.join(layout_path("hosts"), "hosts.yml")) as fh:
        inv = yaml.safe_load(fh) or {}
    return ((inv.get("all") or {}).get("children") or {}).get("bench_hosts") or {}


def bench_hosts():
    """{host: (ssh user, ssh address)} from the site's `hosts` layout (bench_hosts group)."""
    group = _bench_hosts_group()
    user = (group.get("vars") or {}).get("ansible_user")
    out = {}
    for h, v in (group.get("hosts") or {}).items():
        v = v or {}
        out[h] = (v.get("ansible_user", user), v.get("ansible_host", h))
    return out


def local_hosts():
    """The bench hosts in the site's `hosts` layout that are this machine (ansible_connection:
    local, on the host or the group): a workstation that is its own bench host."""
    group = _bench_hosts_group()
    conn = (group.get("vars") or {}).get("ansible_connection")
    return {h for h, v in (group.get("hosts") or {}).items()
            if (v or {}).get("ansible_connection", conn) == "local"}


#: devreg.yml keys that are paths relative to the layout.
LAYOUT_PATH_KEYS = ("registry", "catalog_dir", "rf_ceiling")


def state_view(layout_dir, where):
    """The site a layout lives in, with that layout copied so it can be changed: at
    `<where>/site/`, links to everything of the site but its `layouts/`, links to its other
    layouts, and the layout's own files copied (not linked: they are the record of the state)
    with the relative paths of its devreg.yml made absolute. Ansible reads the copy as it reads
    the layout, and its layers, library tests and `from:` layouts resolve as they do there.
    Returns the copy's directory (a campaign state, docs/ARCHITECTURE.md#runs-tests-and-campaigns)."""
    import shutil  # pylint: disable=import-outside-toplevel
    import yaml  # pylint: disable=import-outside-toplevel
    layout_dir = os.path.abspath(layout_dir)
    root = os.path.dirname(os.path.dirname(layout_dir))
    name = os.path.basename(layout_dir)
    view = os.path.join(where, "site")
    os.makedirs(os.path.join(view, "layouts"))
    for entry in sorted(os.listdir(root)):
        if entry != "layouts":
            os.symlink(os.path.join(root, entry), os.path.join(view, entry))
    for other in sorted(os.listdir(os.path.join(root, "layouts"))):
        if other != name:
            os.symlink(os.path.join(root, "layouts", other), os.path.join(view, "layouts", other))
    mine = os.path.join(view, "layouts", name)
    shutil.copytree(layout_dir, mine, symlinks=False)
    with open(os.path.join(mine, "devreg.yml")) as f:
        cfg = yaml.safe_load(f) or {}
    for k in LAYOUT_PATH_KEYS:
        v = cfg.get(k)
        if v and not os.path.isabs(os.path.expanduser(v)):
            cfg[k] = os.path.normpath(os.path.join(layout_dir, v))
    with open(os.path.join(mine, "devreg.yml"), "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    return mine
