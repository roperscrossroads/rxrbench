#!/usr/bin/env python3
"""where.py <layout>: the directory of a layout, from the site first, then the examples.
where.py --source <family> tree|workspace: where that firmware's source is on this machine."""
import os
import sys

import collections_path  # noqa: E402,F401  (the collections first on sys.path)
from ansible_collections.rxrbench.core.plugins.module_utils import site  # noqa: E402

site.load_env()
if len(sys.argv) == 4 and sys.argv[1] == "--source":
    # where.py --source <family> tree|workspace: a firmware family's source on this machine
    try:
        print(site.firmware_source(sys.argv[2])[sys.argv[3]])
    except KeyError as e:
        sys.exit(str(e))
    sys.exit(0)
try:
    print(site.layout_path(sys.argv[1]))
except IndexError:
    sys.exit(__doc__)
except FileNotFoundError as e:
    # A layer has no boards: `just apply base` must say so, not look for devreg.yml in it.
    from ansible_collections.rxrbench.core.plugins.module_utils import layers  # noqa: E402
    if layers.is_layer(sys.argv[1]):
        sys.exit(f"{sys.argv[1]!r} is a layer, not a layout: it has no boards. A layout inherits "
                 f"it with `layers: [{sys.argv[1]}]` in its devreg.yml.")
    sys.exit(str(e))
