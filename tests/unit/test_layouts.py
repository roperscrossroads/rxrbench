"""Every layout in the repo must resolve against the real registry and name real classes."""
import glob
import os

import pytest
import yaml

from ansible_collections.rxrbench.core.plugins.module_utils import artifact, layout, site
from ansible_collections.rxrbench.core.plugins.plugin_utils import families, registry

REPO = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
# Every layout: the site's (when this checkout has one) and the examples (site.py says where).
LAYOUTS = sorted(os.path.dirname(p) for d in site.layout_dirs()
                 for p in glob.glob(os.path.join(d, "*", "devreg.yml")))
CATALOG = layout.load_catalog(*registry.catalog_dirs())
# A checkout with a site must have its registry (conftest loads .env first); a checkout without
# one skips the layouts that need it, never silently.
HAS_SITE = os.path.isdir(site.site_dir())
REAL = os.path.exists(layout.default_registry_path())


def test_there_is_at_least_one_layout():
    assert LAYOUTS


def test_a_site_has_its_registry():
    if HAS_SITE:
        assert REAL, f"a site at {site.site_dir()} but no registry at {layout.default_registry_path()}"


@pytest.mark.plugins
@pytest.mark.skipif(not REAL, reason="no device registry on this machine (no site)")
@pytest.mark.parametrize("d", LAYOUTS, ids=os.path.basename)
def test_layout_resolves(d):
    cfg = yaml.safe_load(open(os.path.join(d, "devreg.yml")))
    r = layout.load_layout(d, cfg)
    assert r["boards"]


@pytest.mark.parametrize("name", sorted(CATALOG["images"]))
def test_every_catalogued_image_is_well_formed(name):
    img = CATALOG["images"][name]
    assert img.get("family") and img.get("board_tags")
    # a flasher core or a loaded plugin brings (flash/common.py FLASHERS, a plugin's FLASHERS);
    # an image of a family no loaded plugin brings names its own plugin's flasher, unchecked here
    if families.known(img["family"]):
        assert img.get("flasher") in families.flashers(), img.get("flasher")
    assert img.get("rf") in ("managed", "unmanaged", "none")
    # a real site's paths must exist (a stale catalog is found here, not at a board's fetch);
    # the example's point at builds a reader does not have
    assert artifact.check_source(img["source"], on_disk=REAL) == []
