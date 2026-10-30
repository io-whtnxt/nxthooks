"""NxtLint's engine: run ruff over paths, parse its findings, as pure calls.

No config resolution here and no verdict formatting — config.py owns the
former, whichever surface is speaking owns the latter. Keeping this module
importable with nothing but the stdlib is what lets the hook executable, the
CLI and any future adapter share one implementation.

Ruff rather than black+isort+flake8: one binary covers all three, and the
~10ms startup is what makes running it on every file write affordable at all.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

RUFF = "ruff"
DEFAULT_TIMEOUT_S = 10

# Pinned, never inherited. Two reasons, both learned the hard way.
#
# Ruff's defaults move: 0.16 enables PL, RUF, EXE and more, so a version bump
# would silently turn an advisory note about a real defect into a paragraph
# about `subprocess.run` missing an explicit `check=`. And ruff resolves
# config from the *target* file's directory, so without this a repo carrying
# `select = ["ALL"]` would answer a one-line write with D100, CPY001 and S607.
#
# F is pyflakes -- undefined name, unused import, redefinition: the defects an
# agent actually introduces, and the class that turns a guard into a silent
# no-op. E9 is syntax and runtime errors. Everything else is style an agent
# cannot act on usefully at the moment it finishes a write.
#
# Passed as --select, which overrides a repo's own `select` while leaving its
# `ignore` and `per-file-ignores` in force, so a project that deliberately
# permits re-export F401s in __init__.py is still respected.
SELECT = "F,E9"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    code: str
    message: str

    def render(self) -> str:
        return f"{Path(self.path).name}:{self.line} {self.code} {self.message}"


def lint_paths(
    paths,
    *,
    ruff: str = RUFF,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> tuple[list[Finding], str | None]:
    """Lint `paths` against the pinned SELECT set. Returns (findings, error).

    An absent ruff is not an error: this is advisory, and a machine without
    the binary should stay silent rather than narrate its own absence on
    every file write.
    """
    paths = [str(p) for p in paths]
    if not paths:
        return [], None
    if shutil.which(ruff) is None:
        return [], None

    argv = [
        ruff, "check",
        "--output-format", "json",
        "--no-cache",
        "--select", SELECT,
        *paths,
    ]

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [], f"ruff failed: {exc}"

    # 0 = clean, 1 = violations found. Anything else is ruff itself failing,
    # and its stderr is the only thing that explains why.
    if proc.returncode not in (0, 1):
        return [], f"ruff exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}"

    try:
        raw = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], f"ruff emitted unparseable JSON: {exc}"

    findings = [
        Finding(
            path=item.get("filename", ""),
            line=(item.get("location") or {}).get("row", 0),
            code=item.get("code", ""),
            message=item.get("message", ""),
        )
        for item in raw
    ]
    return findings, None
