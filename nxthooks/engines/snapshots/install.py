"""Install the snapshot hook shim.

Writes a shim, never a copy. The code that runs is then always the code
the tests ran, and an upgrade is a no-op rather than a stale duplicate
nobody notices is stale.

Same shape as `nxthooks.engines.nxtgrep.install`, deliberately. The shim lives in
the snapshots store's own bin directory, under the `git` state component
that the shadow repos belong to.
"""
from __future__ import annotations

import sys
from pathlib import Path

from nxthooks import _shims as shims

from nxthooks.engines.snapshots.core import store_root
from nxthooks.engines.snapshots.hook import package_root

SHIM_NAME = "snapshot-save-hook.py"

_SHIM = '''#!/usr/bin/env python3
"""Snapshot hook shim (installed copy; canonical source in the nxtos repo at
nxthooks/engines/snapshots/hook.py). Regenerate with `python3 -m nxthooks.engines.snapshots.install`."""
import sys
sys.path.insert(0, {root!r})
from nxthooks.engines.snapshots.hook import main
sys.exit(main())
'''

#: What every installer wrote before the engines moved into nxthooks, with no
#: ownership record. A marker-less shim is adopted only when its bytes are
#: exactly this template for some root, so it must stay spelled out in full.
_PRE_T1281_SHIM = '''#!/usr/bin/env python3
"""Snapshot hook shim (installed copy; canonical source in the nxtos repo at
nxtoskit/snapshots/hook.py). Regenerate with `python3 -m nxtoskit.snapshots.install`."""
import sys
sys.path.insert(0, {root!r})
from nxtoskit.snapshots.hook import main
sys.exit(main())
'''


def default_bin() -> Path:
    """The snapshots store's own bin directory, resolved per call."""
    return store_root() / "bin"


def install_shim(bin_dir: Path | None = None) -> Path:
    """Write the shim into `bin_dir`, creating it if absent. Returns its path."""
    bin_dir = Path(bin_dir) if bin_dir is not None else default_bin()
    root = str(package_root(Path(__file__)))
    return shims.install(bin_dir / SHIM_NAME, _SHIM.format(root=root), prior=_PRE_T1281_SHIM)


def uninstall_shim(bin_dir: Path | None = None) -> None:
    bin_dir = Path(bin_dir) if bin_dir is not None else default_bin()
    shims.uninstall(bin_dir / SHIM_NAME)


def main() -> int:
    if sys.argv[1:] == ["--uninstall"]:
        uninstall_shim()
    elif sys.argv[1:]:
        raise ValueError("Expected no arguments or --uninstall")
    else:
        print(f"installed {install_shim()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
