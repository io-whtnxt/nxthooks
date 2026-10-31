"""What a built wheel actually contains.

Every install today runs from a source checkout, where copytree finds
everything on disk regardless of what packaging declares. A wheel is the
first place the declaration is the only truth.
"""
import shutil, subprocess, sys, zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

REQUIRED = (
    "nxthooks/adapters/run_nxthooks.py",
    "nxthooks/adapters/cc-dispatch.sh",
    "nxthooks/adapters/pi-extension/index.ts",
    "nxthooks/adapters/opencode-plugin/nxthooks.ts",
    "nxthooks/adapters/hermes-plugin/__init__.py",
    "nxthooks/builtin/nxtlint.toml",
)


#: The tree is copied wholesale rather than file by file, so the build reads the
#: same declaration and package data it would in the checkout. These are state,
#: not input.
_COPY_IGNORES = shutil.ignore_patterns("__pycache__", "*.egg-info", "build",
                                       "dist", ".venv", ".pytest_cache",
                                       ".git", ".codegraph")


def _build_artifacts_in_checkout():
    """Build output sitting in the checkout, at either package-layout depth."""
    found = [str(p.relative_to(REPO))
             for pattern in ("*.egg-info", "*/*.egg-info")
             for p in sorted(REPO.glob(pattern))]
    if (REPO / "build").exists():
        found.append("build")
    return found


def _built_wheel_names(tmp_path):
    """Build from a copy of the tree: setuptools writes its metadata beside the
    source it was pointed at, and that source must not be the checkout."""
    source = tmp_path / "checkout"
    shutil.copytree(REPO, source, ignore=_COPY_IGNORES)
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path)],
        cwd=source, check=True, capture_output=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    return set(zipfile.ZipFile(wheel).namelist())


def test_the_wheel_contains_everything_install_reads(tmp_path):
    names = _built_wheel_names(tmp_path)

    missing = [p for p in REQUIRED if p not in names]
    assert missing == [], f"wheel is missing what install reads: {missing}"


def test_the_wheel_contains_no_bytecode_cache(tmp_path):
    """The `adapters/*/**` globs this test's sibling used to require swept in
    whatever __pycache__ happened to sit on the builder's disk alongside the
    adapter source -- a build-machine artifact shipped to every install."""
    names = _built_wheel_names(tmp_path)

    cached = [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert cached == [], f"wheel ships bytecode cache: {cached}"


def test_building_a_wheel_leaves_no_artifact_in_the_checkout(tmp_path):
    """An `nxthooks.egg-info` in the checkout shadows the installed distribution
    while the checkout leads `sys.path`, so `install_shape()` starts answering
    `dev` halfway through a run -- which fails `test_projection`'s shape record
    on a clean checkout, and only there.
    """
    _built_wheel_names(tmp_path)

    assert _build_artifacts_in_checkout() == []
