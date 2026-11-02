#!/usr/bin/env python3
"""simbench.py --run-dir DIR [--registry FILE] BOARD... : a bench of this plugin's simulated boards.

Core's simulated bench (core/sim/simbench.py) with this plugin's sim registry as the default:
the real firmware on native_sim behind the same serial broker a bench host runs.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, *[".."] * 5))
sys.path.insert(0, os.path.join(REPO, "collections"))
from ansible_collections.rxrbench.core.sim import simbench  # noqa: E402  pylint: disable=wrong-import-position


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--registry" not in argv:
        argv = ["--registry", os.path.join(HERE, "registry.yaml")] + argv
    simbench.main(argv)


if __name__ == "__main__":
    main()
