"""What a built wheel actually contains.

Every install today runs from a source checkout, where copytree finds
everything on disk regardless of what packaging declares. A wheel is the
first place the declaration is the only truth.
"""
import subprocess, sys, zipfile
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


def _built_wheel_names(tmp_path):
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path)],
        cwd=REPO, check=True, capture_output=True,
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
