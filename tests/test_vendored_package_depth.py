"""How the vendored packages locate their own root, and how that fails.

`nxtlint` ships inside `io-whtnxt/nxthooks` as well as here, so it has to find
the directory `nxthooks` is importable from without being told.

A correction to the release spec, which says counting parents breaks under
extraction: it does not, for this shape. `parents[2]` and a marker walk agree
for *every* relocation of the package as a unit -- `vendor/nxthooks/engines/nxtlint/`,
`a/b/c/nxthooks/engines/nxtlint/`, anywhere -- because moving the tree does not change
how deep `hook.py` sits inside it. Measured, not assumed. The real coupling to
extraction is the import statements, `from nxthooks.engines.nxtlint.engine import ...`,
which change with the namespace and fail loudly when they do.

What the marker actually buys is the failure mode, and that is worth having on
this component specifically:

  - counting resolves to a real directory that merely lacks the package, so
    the import fails at hook-spawn time, in a subprocess whose stderr an
    operator may never read, under a `fail = "open"` policy that turns the
    crash into an allow -- a hook that has silently stopped running looks
    exactly like one that ran and found nothing
  - the marker raises where it cannot answer

The one layout where the two genuinely disagree is a file moving *within* the
package, which is a refactor rather than an extraction. It is pinned below
because it is the case that tells them apart at all.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _vendor(dst: Path, prefix: str) -> Path:
    """Copy `nxthooks` under `prefix`, returning the dir it is importable from."""
    root = dst / prefix if prefix else dst
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        REPO / "nxthooks", root / "nxthooks",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return root


@pytest.mark.parametrize("prefix", ["", "vendor", "a/b/c"])
def test_the_package_is_found_wherever_the_tree_is_relocated(tmp_path, prefix):
    root = _vendor(tmp_path, prefix)
    proc = subprocess.run(
        [sys.executable, "-c",
         "from nxthooks.engines.nxtlint.hook import package_root; print(package_root())"],
        capture_output=True, text=True,
        # cwd matters: `python -c` puts the working directory first on
        # sys.path, ahead of PYTHONPATH, so running from the repo would import
        # the real package and the vendored copy would never be exercised.
        cwd=str(root.parent),
        env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"}, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == root


def test_the_hook_bootstraps_when_run_as_a_plain_script(tmp_path):
    """The bootstrap's actual job: `sys.executable hook.py`, no package context.

    This is how the dispatcher spawns it, and the path on which a wrong root
    fails silently rather than loudly.
    """
    root = _vendor(tmp_path / "script", "vendor")
    hook = root / "nxthooks" / "engines" / "nxtlint" / "hook.py"
    proc = subprocess.run(
        [sys.executable, str(hook)],
        input='{"cwd":"/tmp","tool_name":"Read","tool_input":{}}',
        capture_output=True, text=True, cwd="/tmp",
        env={"PATH": "/usr/bin:/bin"}, timeout=60,
    )
    assert proc.returncode == 0, (
        "the hook could not bootstrap itself from a vendored layout:\n" + proc.stderr
    )


def test_it_answers_from_a_file_deeper_inside_the_package(tmp_path):
    """The one case that tells a marker from a count.

    `parents[2]` of `nxthooks/engines/nxtlint/internal/hook.py` is `nxthooks/` -- a
    real directory, with no `nxthooks` package inside it, so the insert
    succeeds and the import then fails. The marker keeps answering `root`.
    """
    root = _vendor(tmp_path / "deeper", "")
    nested = root / "nxthooks" / "engines" / "nxtlint" / "internal"
    nested.mkdir(parents=True)
    probe = nested / "hook.py"
    shutil.copy(root / "nxthooks" / "engines" / "nxtlint" / "hook.py", probe)

    sys.path.insert(0, str(REPO))
    from nxthooks.engines.nxtlint.hook import package_root

    assert package_root(probe) == root
    assert probe.resolve().parents[2] != root, (
        "this test no longer discriminates: counting and the marker now agree"
    )


def test_a_layout_with_no_package_raises(tmp_path):
    """Silence is the defect. A root it cannot find must say so, not guess."""
    stray = tmp_path / "nowhere" / "nxtlint"
    stray.mkdir(parents=True)
    shutil.copy(REPO / "nxthooks" / "engines" / "nxtlint" / "hook.py", stray / "hook.py")

    sys.path.insert(0, str(REPO))
    from nxthooks.engines.nxtlint.hook import package_root

    with pytest.raises(RuntimeError, match="nxthooks/__init__.py"):
        package_root(stray / "hook.py")
