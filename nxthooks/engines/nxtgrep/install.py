"""Install NxtGrep's hook shim.

Writes a shim, never a copy. The code that runs is then always the code the
tests ran, and an upgrade is a no-op rather than a stale duplicate nobody
notices is stale.

This replaces the relevant part of `scripts/nxtgrep-install.py`, a 513-line
script outside the package that also copied the core files, copied the pattern
pack out of the NxtOS hook tree, edited `~/.claude/settings.json` directly,
and kept its own versioned backups with rollback. All of that predates
NxtHooks owning the wiring: the `secret-scan` manifest and the dispatcher do
it now, for every harness rather than for Claude Code alone. What is left is
this.

Same shape as `nxthooks.engines.nxtlint.install`, deliberately -- two builtin hooks
with two different installation stories is one story too many.
"""

from __future__ import annotations

import sys
from pathlib import Path

from nxthooks import _shims as shims

from nxthooks import _paths as paths
from nxthooks.engines.nxtgrep.hook import package_root

SHIM_NAME = "nxtgrep-hook.py"

_SHIM = '''#!/usr/bin/env python3
"""NxtGrep hook shim (installed copy; canonical source in the nxtos repo at
nxthooks/engines/nxtgrep/hook.py). Regenerate with `python3 -m nxthooks.engines.nxtgrep.install`."""
import sys
sys.path.insert(0, {root!r})
from nxthooks.engines.nxtgrep.hook import main
sys.exit(main())
'''

#: What every installer wrote before the engines moved into nxthooks, with no
#: ownership record. A marker-less shim is adopted only when its bytes are
#: exactly this template for some root, so it must stay spelled out in full.
_PRE_T1281_SHIM = '''#!/usr/bin/env python3
"""NxtGrep hook shim (installed copy; canonical source in the nxtos repo at
nxtoskit/nxtgrep/hook.py). Regenerate with `python3 -m nxtoskit.nxtgrep.install`."""
import sys
sys.path.insert(0, {root!r})
from nxtoskit.nxtgrep.hook import main
sys.exit(main())
'''


def default_bin() -> Path:
    """NxtGrep's own component directory, resolved rather than joined.

    The shim used to land in `~/.nxtos/bin`, which NxtOS's path map assigns to
    the `git` component -- so NxtGrep was writing into NxtGit's tree. Resolved
    per call: a module-level constant would freeze HOME at import, which is
    wrong in the installer, the desktop app and the test suite alike.
    """
    return paths.resolve("grep", "data", "bin")


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
