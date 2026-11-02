"""Puts the collections the tools import first on sys.path: RXRBENCH_COLLECTIONS when it is set
(`tools/core_only.py` sets it to a tree with core and a fixture plugin alone, core-portability
P1), else the repository's own. A tool imports this before any collection:

    import collections_path  # noqa: E402,F401
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTIONS = os.environ.get("RXRBENCH_COLLECTIONS") or os.path.join(REPO, "collections")
if COLLECTIONS not in sys.path:
    sys.path.insert(0, COLLECTIONS)
