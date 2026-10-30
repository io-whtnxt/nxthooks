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

from nxthooks.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
