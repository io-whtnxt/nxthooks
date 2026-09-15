"""Put NxtLint's hook source where its manifest says it lives.

Writes a shim, never a copy. `~/.nxtos/bin` is outside every working tree by
design — a guard inside the repo it polices vanishes when you check out a
branch older than itself — but a *copied* hook.py there would be a second
implementation drifting from the first with nothing to notice. The shim only
records where the package is, so the code that runs is always the code the
tests ran.
"""

import sys
from pathlib import Path

from nxthooks._vendor import paths
from nxthooks._vendor.nxtlint.hook import package_root

SHIM_NAME = "nxtlint-hook.py"


def default_bin() -> Path:
    """NxtLint own component directory, resolved rather than joined.

    This used to be `Path.home() / ".nxtos" / "bin"`. That root is mapped to
    the `git` component -- it is where NxtGit guards live -- so NxtLint was
    writing into a neighbour tree, and `test_paths` flags any module that
    builds an NxtOS-owned root itself instead of asking `paths`.

    Resolved per call: a module-level constant would freeze HOME at import,
    which is wrong in the installer, the desktop app and the test suite alike.
    """
    return paths.resolve("lint", "data", "bin")

_SHIM = '''#!/usr/bin/env python3
"""NxtLint hook shim (installed copy; canonical source in the nxtos repo at
nxthooks._vendor/nxtlint/hook.py). Regenerate with `python3 -m nxthooks._vendor.nxtlint.install`."""
import sys
sys.path.insert(0, {root!r})
from nxthooks._vendor.nxtlint.hook import main
sys.exit(main())
'''


def install_shim(bin_dir: Path | None = None) -> Path:
    """Write the shim into `bin_dir`, creating it if absent. Returns its path."""
    bin_dir = Path(bin_dir) if bin_dir is not None else default_bin()
    bin_dir.mkdir(parents=True, exist_ok=True)
    # One definition, shared with the runtime bootstrap: the value baked into
    # the shim and the value hook.py computes for itself must not be able to
    # drift apart.
    root = str(package_root(Path(__file__)))
    target = bin_dir / SHIM_NAME
    target.write_text(_SHIM.format(root=root), encoding="utf-8")
    target.chmod(0o755)
    return target


def main() -> int:
    print(f"installed {install_shim()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
