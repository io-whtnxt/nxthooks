"""The release build step (T1282): the shared nxtos pipeline builds this repo reproducibly.

Drives the shared script directly; the wrapper's argv shape and thinness are
covered by tests/test_release_contract.py.
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

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHARED = Path(os.environ.get("NXTOS_REPO", Path.home() / "Coding" / "nxtos")) / "scripts" / "plugin_release_build.py"


def _source_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if "version" in project:
        return project["version"]
    init = (ROOT / "nxthooks" / "__init__.py").read_text()
    return next(ast.literal_eval(n.value) for n in ast.parse(init).body
                if isinstance(n, ast.Assign) and n.targets[0].id == "__version__")


_TOOLING = subprocess.run(["python3", "-c", "import build, setuptools, wheel"], capture_output=True).returncode == 0


@pytest.mark.skipif(not SHARED.is_file(), reason="nxtos checkout not found (set NXTOS_REPO)")
@pytest.mark.skipif(not _TOOLING, reason="python3 lacks build, setuptools or wheel")
def test_build_is_reproducible_and_records_provenance(tmp_path):
    out = tmp_path / "out"
    # --allow-dirty: a working tree under edit still builds its committed HEAD.
    proc = subprocess.run([sys.executable, "-I", str(SHARED), "build", "--repo", str(ROOT),
                           "--repo-name", "nxthooks", "--out", str(out), "--allow-dirty"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    version = _source_version()
    record = json.loads((out / f"nxthooks-{version}.provenance.json").read_text())
    assert (record["repo"], record["version"], record["reproducible"]) == ("nxthooks", version, True)
    assert re.fullmatch(r"[0-9a-f]{40}", record["revision"])
    by_kind = {a["kind"]: a for a in record["artifacts"]}
    assert (out / by_kind["wheel"]["file"]).is_file() and (out / by_kind["sdist"]["file"]).is_file()
    printed = json.loads(proc.stdout)
    assert printed["wheel"] == by_kind["wheel"]["sha256"] and printed["sdist"] == by_kind["sdist"]["sha256"]
    check = subprocess.run(["python3", "-I", str(SHARED), "verify", "--provenance", str(out / f"nxthooks-{version}.provenance.json")],
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
