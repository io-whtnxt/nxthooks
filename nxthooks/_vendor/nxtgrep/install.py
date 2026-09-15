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

Same shape as `nxthooks._vendor.nxtlint.install`, deliberately -- two builtin hooks
with two different installation stories is one story too many.
"""

from __future__ import annotations

import sys
from pathlib import Path

from nxthooks._vendor import paths
from nxthooks._vendor.nxtgrep.hook import package_root

SHIM_NAME = "nxtgrep-hook.py"

_SHIM = '''#!/usr/bin/env python3
"""NxtGrep hook shim (installed copy; canonical source in the nxtos repo at
nxthooks._vendor/nxtgrep/hook.py). Regenerate with `python3 -m nxthooks._vendor.nxtgrep.install`."""
import sys
sys.path.insert(0, {root!r})
from nxthooks._vendor.nxtgrep.hook import main
sys.exit(main())
'''


def default_bin() -> Path:
    """NxtGrep's own component directory, resolved rather than joined.

    The shim used to land in `~/.nxtos/bin`, which `nxthooks._vendor.paths` maps to
    the `git` component -- so NxtGrep was writing into NxtGit's tree. Resolved
    per call: a module-level constant would freeze HOME at import, which is
    wrong in the installer, the desktop app and the test suite alike.
    """
    return paths.resolve("grep", "data", "bin")


def install_shim(bin_dir: Path | None = None) -> Path:
    """Write the shim into `bin_dir`, creating it if absent. Returns its path."""
    bin_dir = Path(bin_dir) if bin_dir is not None else default_bin()
    bin_dir.mkdir(parents=True, exist_ok=True)
    # One definition of the root, shared with the runtime bootstrap: the value
    # baked in here and the one hook.py computes must not drift apart.
    root = str(package_root(Path(__file__)))
    target = bin_dir / SHIM_NAME
    target.write_text(_SHIM.format(root=root), encoding="utf-8")
    target.chmod(0o755)
    return target


def main() -> int:
    target = install_shim()
    print(f"installed {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
