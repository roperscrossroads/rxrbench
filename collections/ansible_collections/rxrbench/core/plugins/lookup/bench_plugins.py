"""Lookup: the plugin collections a site loads, and files in them."""

DOCUMENTATION = r"""
name: bench_plugins
short_description: The site's plugin collections, or files they hold
description:
  - With no term, returns the plugin collections the site loads (C(<site>/plugins.yml) or
    C(RXRBENCH_PLUGINS)), each as C({name, path}), where C(path) is the collection's directory.
  - With terms, each a path or glob relative to a collection's directory, returns the files that
    match in any loaded collection, in the order the collections are loaded. This is how a role
    finds what a plugin wants on a bench host (C(host/dialects.d/*.json), a tool).
  - With C(what=families), returns the device families as data, one table
    (C(module_utils/familyspec.py)), which is what a module on a bench host takes as
    C(families) in a play that has no layout to publish C(bench_families).
  - With C(what=host), returns what the plugins need on a bench host, merged from each one's
    C(host/host.yml), as one dict. C(venvs) maps a venv's name to C({imports, lock}), where
    C(lock) is its lockfile (C(host/venvs/<name>.txt) of the plugin); C(downloads) is a list of
    C({url, sha256, dest}).
options:
  what:
    description: C(families) for the families table, C(host) for the plugins' bench-host needs,
      C(boards) for the board types (this collection's, each plugin's and the site's, later
      over earlier), C(images) for the firmware catalog's images (the same directories),
      C(usb_kinds) for the USB ids the plugins bring, instead of collections or files.
    type: str
    choices: [families, host, boards, images, usb_kinds]
  _terms:
    description: Paths or globs relative to a plugin collection's directory.
    type: list
    elements: str
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: The console rules every loaded plugin brings
  ansible.builtin.debug:
    msg: "{{ query('rxrbench.core.bench_plugins', 'host/dialects.d/*.json') }}"
"""

RETURN = r"""
_list:
  description: The collections as C({name, path}), or the matching files' paths.
  type: list
"""

import glob
import os

import yaml

from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase

from ansible_collections.rxrbench.core.plugins.module_utils import site
from ansible_collections.rxrbench.core.plugins.plugin_utils import registry


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        site.load_env()
        self.set_options(var_options=variables, direct=kwargs)
        if self.get_option("what") == "families":
            from ansible_collections.rxrbench.core.plugins.plugin_utils import families  # pylint: disable=import-outside-toplevel
            return [families.specs()]
        if self.get_option("what") == "usb_kinds":
            from ansible_collections.rxrbench.core.plugins.plugin_utils import families  # pylint: disable=import-outside-toplevel
            return [families.usb_kinds()]
        if self.get_option("what") in ("boards", "images"):
            # the board types and the listed images: this collection's, each plugin's, the
            # site's, later over earlier
            from ansible_collections.rxrbench.core.plugins.module_utils import layout  # pylint: disable=import-outside-toplevel
            return [layout.load_catalog(*registry.catalog_dirs())[self.get_option("what")]]
        try:
            dirs = registry.dirs(site.plugins())
        except registry.PluginError as e:
            raise AnsibleError(str(e)) from e
        if self.get_option("what") == "host":
            out = {"venvs": {}, "downloads": []}
            for path in dirs.values():
                manifest = os.path.join(path, "host", "host.yml")
                if not os.path.exists(manifest):
                    continue
                with open(manifest) as fh:
                    doc = yaml.safe_load(fh) or {}
                for name, venv in (doc.get("venvs") or {}).items():
                    out["venvs"][name] = {**venv, "lock": os.path.join(path, "host", "venvs", name + ".txt")}
                out["downloads"] += doc.get("downloads") or []
            return [out]
        if not terms:
            return [{"name": name, "path": path} for name, path in dirs.items()]
        return [f for path in dirs.values() for term in terms
                for f in sorted(glob.glob(os.path.join(path, term)))]
