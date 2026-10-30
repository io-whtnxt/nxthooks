"""NxtLint's CLI — the blocking half of the pair.

The hook advises on one file as it lands and can never refuse. This gates the
whole staged set as it becomes permanent, and here a finding is allowed to
stop something: a commit is a coherent unit, not a half-finished edit, and a
human is standing there to act on the report.

Repo-agnostic on purpose. `nxtlint --staged` asks git what is staged rather
than being told, so wiring it is one line in any project's pre-commit hook
instead of a bespoke gate written per repository.
"""

import argparse
import sys
from pathlib import Path

from nxthooks._safe_git import run_git

from .engine import lint_paths


def staged_python(cwd: Path) -> list[Path]:
    """Staged, still-present .py files. Deletions are excluded by ACM."""
    proc = run_git(["diff", "--cached", "--name-only", "--diff-filter=ACM"], cwd, timeout=30)
    if proc.outcome != "ok":
        return []
    return [
        path
        for name in proc.text.split("\n")
        if name.strip().endswith(".py")
        and (path := cwd / name.strip()).is_file()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nxtlint", description="Lint Python the way NxtLint's hook does."
    )
    parser.add_argument("paths", nargs="*", help="files to lint")
    parser.add_argument(
        "--staged", action="store_true", help="lint the git-staged Python files"
    )
    parser.add_argument("--cwd", default=".", help="repository root for --staged")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).resolve()
    targets = [Path(p) for p in args.paths]
    if args.staged:
        targets += staged_python(cwd)
    if not targets:
        return 0

    findings, error = lint_paths(targets)
    if error:
        # Fail open, loudly. A linter that cannot run is not evidence that the
        # code is bad, and blocking a commit on our own breakage is the one
        # outcome nobody can act on.
        print(f"nxtlint: {error}", file=sys.stderr)
        return 0
    if not findings:
        return 0

    for finding in findings:
        print(f"{finding.path}:{finding.line} {finding.code} {finding.message}")
    print(f"\nnxtlint: {len(findings)} finding(s).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
