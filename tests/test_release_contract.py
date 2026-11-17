"""Release contract (T1282): what nxtos scripts/plugin_release.py reads from this repo.

Covers the three pieces Spec 2 delivers here: the `[tool.nxt-release]`
config fields (contract 2.4), the single version source that `--version`
must print (invariant I-4), and the thin `scripts/release.sh` wrapper's
argv shape and thinness (invariant I-1).
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
CFG = PYPROJECT["tool"]["nxt-release"]
WRAPPER = ROOT / "scripts" / "release.sh"


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
    assert set(CFG["bins"]) <= set(scripts), "every bin must be a [project.scripts] key"
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

def test_wrapper_replaces_release_build_and_is_thin():
    assert not (ROOT / "scripts" / "release-build.sh").exists()
    assert os.access(WRAPPER, os.X_OK)
    text = WRAPPER.read_text()
    assert len(text.splitlines()) < 20
    for banned in ("git tag", "gh ", "PyInstaller"):
        assert banned not in text, f"release logic does not belong in the wrapper: {banned!r}"


def _run_wrapper(tmp_path, *args):
    scripts = tmp_path / "nxtos" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "plugin_release.py").write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    proc = subprocess.run([str(WRAPPER), *args], capture_output=True, text=True,
                          env={**os.environ, "NXTOS_REPO": str(tmp_path / "nxtos")})
    return proc


def test_wrapper_argv_per_stage(tmp_path):
    out = str(tmp_path / "out")
    cases = {
        ("candidate", "--out", out): ["candidate", "--repo", str(ROOT), "--out", out],
        ("bump", "--part", "patch"): ["bump", "--repo", str(ROOT), "--part", "patch"],
        ("hosted", "--release", "rec.json"): ["hosted", "--release", "rec.json"],
        ("publish", "--release", "rec.json"): ["publish", "--release", "rec.json"],
        (): ["--repo", str(ROOT)],
    }
    for args, expected in cases.items():
        proc = _run_wrapper(tmp_path, *args)
        assert proc.returncode == 0, f"{args}: {proc.stderr}"
        assert json.loads(proc.stdout) == expected, args


def test_unknown_stage_is_refused(tmp_path):
    proc = _run_wrapper(tmp_path, "publish-to-pypi")
    assert proc.returncode == 2 and "unknown stage" in proc.stderr


def test_missing_shared_script_fails_with_a_pointer(tmp_path):
    proc = subprocess.run([str(WRAPPER), "candidate", "--out", str(tmp_path)],
                          capture_output=True, text=True,
                          env={**os.environ, "NXTOS_REPO": str(tmp_path / "absent")})
    assert proc.returncode == 1 and "NXTOS_REPO" in proc.stderr


# --- no package-index publishing (operator decision 2026-11-16) -------------

def test_no_pypi_publish_workflow():
    assert not (ROOT / ".github" / "workflows" / "publish.yml").exists()
