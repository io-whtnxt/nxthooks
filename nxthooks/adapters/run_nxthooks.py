#!/usr/bin/env python3
"""Standalone launcher for the installed NxtHooks runtime copy.

Lives at <root>/lib/run_nxthooks.py so harness transports never need to
know where the repo is — the installed copy under [install].root is the
whole runtime, per the standalone shipping model.
"""

import sys
from pathlib import Path

_lib = Path(__file__).resolve().parent
sys.path.insert(0, str(_lib))
# Appended, never inserted: `_framework` carries a partial `nxthooks._vendor` (paths
# and projection only) for a machine that has no full one. Ahead of the path
# it would shadow a complete installation, and `nxthooks install` and
# `nxthooks uninstall` -- reachable from this entrypoint, which exposes the
# whole CLI -- both import `nxthooks._vendor.plugins.delivery`.
sys.path.append(str(_lib / "_framework"))

from nxthooks.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
