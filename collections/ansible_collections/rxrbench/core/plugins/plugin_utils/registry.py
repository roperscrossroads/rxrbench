"""The plugin collections a site loads, found by name.

A plugin is an Ansible collection with `plugins/plugin_utils/bench.py`, which exports

    FAMILIES  {family: row}     the device families it brings (families/__init__.py: the fields)
    PLUGINS   {name: Family()}  optional: the objects its rows name as `plugin` (families/base.py)

Its `board` role holds the task files the core plays include for a board of its families, named
by the row's own words: `<vocabulary>.yml` (config), `facts_<vocabulary>.yml`, `update_<word>.yml`,
`secret_<kind>[_<via>]_read.yml` and `..._write.yml` for each of its secrets, `locate_<led>.yml`.

This code runs on the controller only (the inventory plugin, the tools): a module on a bench
host gets what it needs from a plugin as data, never by importing one.
"""
import importlib
import os
import sys


class PluginError(Exception):
    pass


def _roots(extra=()):
    from ..module_utils import site  # pylint: disable=import-outside-toplevel
    # the tools' collections tree (tools/collections_path.py), then the site's
    ours = os.environ.get("RXRBENCH_COLLECTIONS") or os.path.join(site.REPO, "collections")
    return [ours, os.path.join(site.site_dir(), "collections"),
            *extra]


def load(names, roots=()):
    """{collection: its bench module} for each name (`namespace.collection`), in order.
    `roots` are further directories holding an `ansible_collections/` tree. Under Ansible the
    configured collections path is what finds them; elsewhere these directories are."""
    for root in _roots(roots):
        if os.path.isdir(root) and root not in sys.path:
            sys.path.append(root)
    out, owner = {}, {}
    for name in names:
        if name.count(".") != 1:
            raise PluginError(f"plugin {name!r}: a plugin is named namespace.collection")
        try:
            mod = importlib.import_module(f"ansible_collections.{name}.plugins.plugin_utils.bench")
        except ImportError as e:
            raise PluginError(f"plugin {name}: {e}") from e
        if not isinstance(getattr(mod, "FAMILIES", None), dict):
            raise PluginError(f"plugin {name}: plugin_utils/bench.py exports no FAMILIES")
        for family in mod.FAMILIES:
            if family in owner:
                raise PluginError(f"family {family!r} is brought by both {owner[family]} and {name}")
            owner[family] = name
        out[name] = mod
    return out


def dirs(names, roots=()):
    """{collection: its directory} for each plugin collection named."""
    return {name: os.path.abspath(os.path.join(os.path.dirname(mod.__file__), "..", ".."))
            for name, mod in load(names, roots).items()}


def catalog_dirs():
    """The catalog directories, in the order they are layered: core's, each loaded plugin's,
    the site's. (A plugin brings the board types its devices are.)"""
    from ..module_utils import site  # pylint: disable=import-outside-toplevel
    theirs = [os.path.join(d, "catalog") for d in dirs(site.plugins()).values()]
    return [site.CATALOG, *[d for d in theirs if os.path.isdir(d)],
            os.path.join(site.site_dir(), "catalog")]
