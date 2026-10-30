#!/usr/bin/env python3
"""Snapshots as a NxtHooks source: save before a risky write, silently.

The dispatcher feeds the normalized envelope on stdin and mirrors the
essentials into NXTHOOKS_* env vars. This source saves at most one
snapshot per project per debounce window and exits 0 with no output on
success: a CTX line per write would tax the same context window the
budgets protect. Anything on stderr with a nonzero exit surfaces through
the dispatcher's fail-open path, which is exactly where a broken safety
net should be announced.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PACKAGE_MARKER = "nxthooks"


def package_root(start: Path | None = None) -> Path:
    """The directory `nxthooks` is importable from, found by marker.

    Same reasoning as nxtgrep's: the dispatcher may spawn this with no
    guarantee that nxthooks is on sys.path, and a miscounted root resolves
    to a real directory that merely lacks the package -- so the import
    fails inside a subprocess nobody reads, under a policy that turns the
    crash into an allow. Raising is the point.
    """
    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        if (parent / PACKAGE_MARKER / "__init__.py").is_file():
            return parent
    raise RuntimeError(
        f"no directory above {here} contains {PACKAGE_MARKER}/__init__.py; "
        "snapshots cannot locate the package they belong to"
    )


sys.path.insert(0, str(package_root()))

from nxthooks.engines.snapshots.core import (  # noqa: E402
    SnapshotError,
    due_for_save,
    save,
    should_snapshot,
)

ALLOW = 0
ERROR = 1


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError) as exc:
        print(f"snapshot-save: could not parse its input payload: {exc}", file=sys.stderr)
        return ERROR
    if not isinstance(payload, dict):
        print("snapshot-save: hook payload was not an object", file=sys.stderr)
        return ERROR
    cwd = os.environ.get("NXTHOOKS_CWD") or payload.get("cwd") or ""
    if not should_snapshot(cwd):
        return ALLOW
    if not due_for_save(cwd):
        return ALLOW
    tool = os.environ.get("NXTHOOKS_TOOL") or payload.get("tool") or "write"
    try:
        save(cwd, label=f"before {tool}")
    except SnapshotError as exc:
        print(f"snapshot-save: {exc}", file=sys.stderr)
        return ERROR
    return ALLOW


if __name__ == "__main__":
    sys.exit(main())
