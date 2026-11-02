"""Inventory: a layout's boards, resolved against the site's device registry."""

DOCUMENTATION = r"""
name: devreg
short_description: Bench boards from a layout and the site's device registry
description:
  - Turns a layout directory into an inventory. The layout's C(devreg.yml) names the boards and
    the bench host each one is plugged into. Identity (USB serial, board type, policy) comes
    from the site's device registry, which stays the single source of truth for it.
  - Each board is a host whose connection is its bench host. The bench hosts are hosts too, in
    the C(bench_hosts) group.
  - "Groups are derived from each board's declared state in its host_vars file:
    C(family_NAME), a group for the value of its family's identity key, C(feature_NAME),
    the groups its family's plugin adds, C(host_BENCHHOST), C(absent) and C(no_console)."
  - The layout is checked while it is parsed and refused as a whole if a board is unknown to
    the registry, the RF rules are broken, or a family's own check of the layout fails.
  - "A layout may inherit from layers (C(layers:), module_utils/layers.py): each layer's
    group_vars/all.yml is set on group C(all), below the layout's own group_vars; a board's
    C(profile) is set on group C(profile_NAME), composed over the layers' vars, below the
    layout's own files. The go-aheads (C(bench_apply_allowed), C(bench_first_flash))
    are never inherited: a layer that states one refuses the layout."
options:
  plugin:
    description: Must be C(rxrbench.core.devreg).
    required: true
    choices: ['rxrbench.core.devreg']
  registry:
    description: Path to the device registry. Default is C($DEVREG_REGISTRY), then
      the site's (C(<site>/registry.yaml)). A relative path is relative to the layout.
    type: str
  boards:
    description: The boards in this layout, by registry name or alias, each with the bench
      C(host) it is plugged into and, optionally, the C(profile) that declares its state
      and/or C(from) (C(<layout>/<board>)), another layout's declaration of the same board.
    type: dict
    required: true
  layers:
    description: Layers this layout inherits from, in order; a later one wins. Looked up in
      the site's C(layers/), then the tool's C(site.example/layers/).
    type: list
    elements: str
    default: []
  from:
    description: Another layout this one is a variant of. Every board of it that C(boards)
      does not list comes along as declared there (C(from) C(<layout>/<board>)), on the host
      declared there; the boards listed here are this layout's own. Its layers, group_vars
      and go-aheads are not taken.
    type: str
  rf_rules:
    description: The layout's RF rules (C(region), C(max_tx_power), C(band_mhz)); each
      may only tighten the ceiling, which is the site's C(rf_rules.yml) or C(rf_ceiling).
    type: dict
    default: {}
  rf_ceiling:
    description: A file holding this layout's RF ceiling instead of the site's (the simulated
      layouts name one, so a site's ceiling cannot refuse them). Relative to the layout.
    type: str
  catalog_dir:
    description: The directory holding C(firmware.yml), C(boards.yml) and a family's own catalog file. Default is the
      repository's C(catalog/), two levels above the layout. A relative path is relative to
      the layout.
    type: str
"""

EXAMPLES = r"""
# layouts/example/devreg.yml
plugin: rxrbench.core.devreg
layers: [base]            # builds, store, interpreter, profiles: declared once, in layers/base
boards:
  node1: {}
  node2: {host: benchpi1, profile: plain}   # a host is only a hint: the plug watcher's is used
"""

import os

from ansible.errors import AnsibleParserError
from ansible.plugins.inventory import BaseInventoryPlugin
from ansible.template import trust_as_template

from ansible_collections.rxrbench.core.plugins.module_utils import layers as layers_mod
from ansible_collections.rxrbench.core.plugins.module_utils import layout as layout_mod
from ansible_collections.rxrbench.core.plugins.module_utils import site as site_mod


def _trusted(value):
    """`value` with every string in it tagged as a trusted template. A string an inventory
    plugin sets is inert to `{{ }}` (ansible-core 2.19+), unlike one from a group_vars file; a
    layer's files are the site's own YAML, so they get the trust a group_vars file gets."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {k: _trusted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_trusted(v) for v in value]
    return value


class InventoryModule(BaseInventoryPlugin):
    NAME = "rxrbench.core.devreg"

    def verify_file(self, path):
        return super().verify_file(path) and os.path.basename(path) in ("devreg.yml", "devreg.yaml")

    def parse(self, inventory, loader, path, cache=True):
        super().parse(inventory, loader, path, cache)
        config = self._read_config_data(path)
        layout_dir = os.path.dirname(os.path.abspath(path))
        site_mod.load_env()   # the site's settings (.env), as just loads them
        cfg = {
            "registry": self.get_option("registry"),
            "boards": self.get_option("boards"),
            "rf_rules": self.get_option("rf_rules") or {},
            "rf_ceiling": self.get_option("rf_ceiling"),
            "catalog_dir": self.get_option("catalog_dir"),
            "layers": self.get_option("layers") or [],
            "from": self.get_option("from"),
        }
        try:
            resolved = layout_mod.load_layout(layout_dir, cfg)
            layered = layers_mod.resolve(layout_dir, cfg)
        except (layout_mod.LayoutError, layers_mod.LayerError, OSError) as e:
            raise AnsibleParserError(f"{path}: {e}") from e

        for w in resolved["warnings"]:
            self.display.warning(w)

        inventory.add_group("bench_hosts")
        inventory.add_group("boards")
        for host, conn in sorted(resolved["bench_hosts"].items()):
            inventory.add_host(host, group="bench_hosts")
            for k, v in conn.items():
                inventory.set_variable(host, k, v)
        for name, b in sorted(resolved["boards"].items()):
            inventory.add_host(name)
            for g in b["groups"]:
                inventory.add_group(g)
                inventory.add_child(g, name)
            for k, v in {**b["connection"], **b["vars"]}.items():
                inventory.set_variable(name, k, v)
        # Inherited from the layers (module_utils/layers.py). Variables an inventory plugin sets
        # on a group rank below every group_vars/ file, so the layout's own files win; a
        # profile's group is a child of `all`, so it wins over the layers' shared vars.
        # (tests/unit/test_layers.py proves the order against ansible-inventory.)
        for k, v in layered["all_vars"].items():
            inventory.set_variable("all", k, _trusted(v))
        # A family's vars (a layer's group_vars/family_<f>.yml) on the family's group, which
        # the resolved layout put each board of that family in.
        for fam, fv in layered["family_vars"].items():
            g = f"family_{fam}"
            inventory.add_group(g)
            for k, v in fv.items():
                inventory.set_variable(g, k, _trusted(v))
        for board, pname in layered["profile_of"].items():
            if board not in resolved["boards"]:
                continue   # absent from the resolved layout (refused boards never get here)
            g = f"profile_{pname}"
            inventory.add_group(g)
            inventory.add_child(g, board)
            for k, v in layered["profiles"][pname].items():
                inventory.set_variable(g, k, _trusted(v))
        # A board's appends (a layer's boards/<board>.yml) and its `from:`: host variables, above
        # the layout's group_vars file and below its host_vars.
        for board, hv in layered["host_vars"].items():
            if board in resolved["boards"]:
                for k, v in hv.items():
                    inventory.set_variable(board, k, _trusted(v))
        # Where the site is (module_utils/site.py, the one place paths come from): every play
        # and role reads these, never a path of its own.
        inventory.set_variable("all", "bench_repo", site_mod.REPO)
        inventory.set_variable("all", "bench_site_dir", site_mod.site_dir())
        # The registry this layout was resolved against: its own when it names one (a layout
        # may bring one, as the simulated benches do), else the site's.
        own = os.path.expanduser(cfg["registry"]) if cfg["registry"] else None
        inventory.set_variable("all", "bench_registry",
                               (own if os.path.isabs(own) else os.path.normpath(os.path.join(layout_dir, own)))
                               if own else site_mod.registry_path())
        inventory.set_variable("all", "bench_facts_dir", site_mod.facts_dir())
        # The device families as data, for modules on a bench host (module_utils/familyspec.py).
        inventory.set_variable("all", "bench_families", resolved["families"])
        inventory.set_variable("all", "bench_layout", os.path.basename(layout_dir))
        inventory.set_variable("all", "bench_layout_dir", layout_dir)
        inventory.set_variable("all", "bench_layers", [L["name"] for L in layered["layers"]])
        del config
