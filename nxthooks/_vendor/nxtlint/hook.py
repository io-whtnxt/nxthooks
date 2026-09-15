#!/usr/bin/env python3
"""NxtLint's NxtHooks source — the only stdin/stdout protocol code it has.

Policy lives in config.py, scanning in engine.py. This translates one
NxtHooks envelope into an engine call and the engine's findings into CTX
lines, and carries no policy of its own.

It reports by exiting 0 with `CTX:` lines, never by exiting 2. NxtHooks would
happily downgrade an exit-2 block into a note under `policy = "advise"`, but
that puts the never-blocks property in a config field an operator can flip.
Exiting 0 puts it in the code instead, so a linter that refuses an agent's
edit is not a thing this component can become.

Fail-silent in every direction: an unparseable envelope, a path that is not
Python, a file that has since been deleted, a ruff that is not installed.
Advice nobody asked for should cost nothing when it cannot be given.
"""

import json
import sys
from pathlib import Path

#: The top-level package this module ships inside. A vendored build
#: (io-whtnxt/nxthooks) rewrites this to its own root; keeping it a
#: single constant is what makes that a substitution rather than
#: surgery inside the walk.
PACKAGE_MARKER = "nxthooks"


def package_root(start: Path | None = None) -> Path:
    """The directory `nxthooks._vendor` is importable from, found by marker.

    This was `parents[2]` -- nxtlint/ -> nxthooks._vendor/ -> repo. Counting levels
    survives exactly as long as the depth does, and an off-by-one still
    resolves to a *real* directory, one that simply has no nxthooks._vendor in it. The
    import then fails at hook-spawn time, inside a subprocess whose stderr an
    operator may never read, on a component whose `fail = "open"` policy turns
    the crash into an allow. A hook that silently stops running looks exactly
    like a hook that ran and found nothing.

    Raising is the point: a missing marker is unrecoverable here, and saying so
    beats inserting a plausible wrong path.
    """
    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        if (parent / PACKAGE_MARKER / "__init__.py").is_file():
            return parent
    raise RuntimeError(
        f"no directory above {here} contains {PACKAGE_MARKER}/__init__.py; "
        "nxtlint cannot locate the package it belongs to"
    )


# The dispatcher spawns this with `sys.executable hook.py` and no guarantee
# that nxthooks._vendor is importable from site-packages — editable checkouts and
# dogfood machines both break that assumption.
sys.path.insert(0, str(package_root()))

from nxthooks._vendor.nxtlint.engine import lint_paths  # noqa: E402

PATH_KEYS = ("file_path", "notebook_path", "path")

#: Codex carries every edit in one `command` string with no path key at all,
#: so the keys above find nothing and NxtLint was silently inert on every
#: Codex edit. The grammar is read out of the codex binary itself, not
#: guessed: `Begin Patch`, `Add File:`, `Update File:`, `Delete File:`,
#: `Move to:`, `End Patch`.
PATCH_TOOLS = frozenset({"apply_patch"})

#: `Move to:` retargets the `Update File:` above it -- the source path is
#: gone by the time this hook runs, so linting it would find nothing.
PATCH_WRITES = ("*** Add File:", "*** Update File:")
PATCH_MOVE = "*** Move to:"

# Matches NxtGrep's max_reported_hits. Past a handful of findings the agent
# has enough to act on, and every further line is context budget spent to say
# the same thing again.
MAX_REPORTED = 5


def _lintable(raw: str, cwd: Path) -> Path | None:
    """A real .py file, resolving a relative path against the agent's cwd.

    The hook runs as a subprocess whose own cwd is nobody's business; patch
    headers are repo-relative, so resolving against `os.getcwd()` would find
    nothing and say nothing.
    """
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return path if path.suffix == ".py" and path.is_file() else None


def _patch_targets(command: str, cwd: Path) -> list[Path]:
    """Every file an apply_patch body leaves on disk, in order."""
    targets: list[Path] = []
    for line in command.splitlines():
        line = line.strip()
        for header in PATCH_WRITES:
            if line.startswith(header):
                targets.append(Path(line[len(header):].strip()))
                break
        else:
            # `Move to:` replaces the entry it follows rather than adding one.
            if line.startswith(PATCH_MOVE) and targets:
                targets[-1] = Path(line[len(PATCH_MOVE):].strip())

    resolved = [_lintable(str(t), cwd) for t in targets]
    return [p for p in resolved if p is not None]


def target_paths(payload: dict) -> list[Path]:
    """The files this tool call wrote, if it wrote any we can lint."""
    tool_input = payload.get("tool_input") or payload.get("args") or {}
    if not isinstance(tool_input, dict):
        return []
    cwd = Path(payload.get("cwd") or ".")

    tool = str(payload.get("tool") or payload.get("tool_name") or "")
    if tool.lower() in PATCH_TOOLS:
        # Gated on the tool name, not on the text: scanning every Bash
        # `command` for a patch header would let an agent's own echo or
        # heredoc steer the linter at a path it never wrote.
        command = tool_input.get("command")
        return _patch_targets(command, cwd) if isinstance(command, str) else []

    for key in PATH_KEYS:
        raw = tool_input.get(key)
        if isinstance(raw, str) and raw.strip():
            found = _lintable(raw, cwd)
            return [found] if found else []
    return []


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0

    paths = target_paths(payload)
    if not paths:
        return 0

    findings, error = lint_paths(paths)
    if error or not findings:
        return 0

    for finding in findings[:MAX_REPORTED]:
        print(f"CTX: [NxtLint] {finding.render()}")
    if len(findings) > MAX_REPORTED:
        where = paths[0].name if len(paths) == 1 else f"{len(paths)} files"
        print(f"CTX: [NxtLint] +{len(findings) - MAX_REPORTED} more in {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
