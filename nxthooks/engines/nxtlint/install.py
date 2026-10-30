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

from nxthooks import _shims as shims

from nxthooks import _paths as paths
from nxthooks.engines.nxtlint.hook import package_root

SHIM_NAME = "nxtlint-hook.py"


def default_bin() -> Path:
    """NxtLint own component directory, resolved rather than joined.

    This used to be `Path.home() / ".nxtos" / "bin"`. That root is mapped to
    the `git` component -- it is where NxtGit guards live -- so NxtLint was
    writing into a neighbour tree, and `test_paths` flags any module that
    builds an NxtOS-owned root itself instead of asking `paths`.

    Resolved per call: a module-level constant would freeze HOME at import,
    which is wrong in the installer, the desktop app and the test suite alike.
    The shim itself is what is resolved, so a shim an earlier install left in
    `~/.nxtos/bin` is adopted here rather than orphaned there.
    """
    return paths.resolve("lint", "data", f"bin/{SHIM_NAME}").parent

_SHIM = '''#!/usr/bin/env python3
"""NxtLint hook shim (installed copy; canonical source in the nxtos repo at
nxthooks/engines/nxtlint/hook.py). Regenerate with `python3 -m nxthooks.engines.nxtlint.install`."""
import sys
sys.path.insert(0, {root!r})
from nxthooks.engines.nxtlint.hook import main
sys.exit(main())
'''

#: What every installer wrote before the engines moved into nxthooks, with no
#: ownership record. A marker-less shim is adopted only when its bytes are
#: exactly this template for some root, so it must stay spelled out in full.
_PRE_T1281_SHIM = '''#!/usr/bin/env python3
"""NxtLint hook shim (installed copy; canonical source in the nxtos repo at
nxtoskit/nxtlint/hook.py). Regenerate with `python3 -m nxtoskit.nxtlint.install`."""
import sys
sys.path.insert(0, {root!r})
from nxtoskit.nxtlint.hook import main
sys.exit(main())
'''


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
