"""Release contract (T1282): what nxtos scripts/plugin_release.py reads from this repo.

Covers the three pieces Spec 2 delivers here: the `[tool.nxt-release]`
config fields (contract 2.4), the single version source that `--version`
must print (invariant I-4), and the `scripts/release.sh` wrapper's
byte-identity with the shared plugin release wrapper (invariant I-1).
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
CFG = PYPROJECT["tool"]["nxt-release"]
WRAPPER = ROOT / "scripts" / "release.sh"
# The one plugin release wrapper (nxtos scripts/plugin-release-wrapper.sh).
# The contract is byte-identity, not shape: any edit must land in every
# plugin repo at once via the shared source, so we pin the hash.
WRAPPER_SHA256 = "18f60a5e3d7a1e8d15e6ef6ea6d859c91801a5ce6948a0b44d7f51bc908201e9"


def _source_version() -> str:
    init = (ROOT / "nxthooks" / "__init__.py").read_text()
    return next(ast.literal_eval(n.value) for n in ast.parse(init).body
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == "__version__")


# --- [tool.nxt-release] ----------------------------------------------------

def test_plugin_and_bins():
    assert CFG["plugin"] == "nxthooks"
    scripts = PYPROJECT["project"]["scripts"]
    assert CFG["plugin"] in CFG["bins"]
    # The dangerous drift is the other way round: a fourth console script left
    # out of `bins` would silently miss the frozen bundle (review 451a7f42).
    assert set(CFG["bins"]) == set(scripts), "every [project.scripts] key ships, and nothing else"
    # The stranger journey expects exactly these three on PATH.
    assert CFG["bins"] == ["nxthooks", "nxtlint", "nxthooks-pack"]


def test_smoke_and_package_data():
    assert CFG["smoke"] == [["--contract"]]
    pkg_data = PYPROJECT["tool"]["setuptools"]["package-data"]
    assert set(CFG["package_data"]) <= set(pkg_data), "named packages carry the data the freeze collects"


def test_lock_omitted_only_because_the_third_party_set_is_empty():
    resolved = list(PYPROJECT["project"].get("dependencies", []))
    for group in PYPROJECT["project"].get("optional-dependencies", {}).values():
        resolved += group
    assert ("lock" in CFG) == bool(resolved)
    assert "lock" not in CFG and resolved == []


def test_extras_stay_empty():
    assert CFG.get("extras", []) == []


# --- one version source, printed by --version (I-4) -------------------------

def test_version_is_dynamic_from_the_package_init():
    assert "version" in PYPROJECT["project"]["dynamic"]
    assert PYPROJECT["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "nxthooks.__version__"
    assert re.fullmatch(r"\d+\.\d+\.\d+", _source_version())


def test_cli_version_prints_the_version_source():
    proc = subprocess.run([sys.executable, "-m", "nxthooks", "--version"],
                          capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _source_version()


# --- scripts/release.sh (I-1) -----------------------------------------------

def test_release_build_script_is_gone():
    assert not (ROOT / "scripts" / "release-build.sh").exists()


def test_wrapper_is_the_shared_plugin_release_wrapper():
    assert hashlib.sha256(WRAPPER.read_bytes()).hexdigest() == WRAPPER_SHA256
    proc = subprocess.run(["bash", "-n", str(WRAPPER)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# --- no package-index publishing (operator decision 2026-11-16) -------------

def test_no_pypi_publish_workflow():
    assert not (ROOT / ".github" / "workflows" / "publish.yml").exists()
