"""`install` must say which source it is shipping.

`_pkg_dir()` returns the directory of the imported `nxthooks`, which is the
right thing to copy -- install should ship the code it is running. The defect
is that nothing reports which source that resolved to, and the resolution
depends on the caller's working directory: a repo checkout on sys.path wins
over site-packages.

Observed twice on 2026-09-13. The installed dispatcher was replaced from a
branch that predated the `codex` harness, so every Codex tool call came back
"Blocked by hook". It was fixed by reinstalling, then silently regressed when
a later install ran from a directory that resolved to the same stale branch.
Nothing in either run's output named a source, so there was nothing to notice.

This does not constrain which source wins. It makes the answer visible, and
lets it be pinned.
"""

import subprocess
import sys

import pytest

from nxthooks import lifecycle


def test_install_reports_the_source_it_copies_from(hooks_machine, capsys):
    host = hooks_machine()
    lifecycle.cmd_install(_args(), host=host)
    out = capsys.readouterr().out
    assert "source" in out, "install said nothing about where the runtime came from"
    assert str(lifecycle._pkg_dir()) in out, (
        "the reported source must be the actual package directory copied, "
        "not a label"
    )


def test_the_reported_source_is_what_actually_landed(hooks_machine, capsys):
    """A report that can disagree with the bytes on disk is worse than none."""
    host = hooks_machine()
    root = lifecycle.cmd_install(_args(), host=host) and None
    out = capsys.readouterr().out
    reported = _reported_source(out)
    installed = host.home / "rt" / "lib" / "nxthooks" / "__init__.py"
    if not installed.exists():
        installed = _find_installed_init(host)
    assert installed and installed.exists()
    assert (reported / "__init__.py").read_text() == installed.read_text(), (
        "install reported a source whose bytes are not what it installed"
    )


def test_a_pinned_source_overrides_resolution(hooks_machine, tmp_path, capsys):
    """--from is the escape hatch for a machine where sys.path resolution
    picks up a checkout nobody meant to ship."""
    host = hooks_machine()
    args = _args()
    args.source = str(lifecycle._pkg_dir())
    lifecycle.cmd_install(args, host=host)
    assert str(lifecycle._pkg_dir()) in capsys.readouterr().out


def test_the_source_line_names_the_branch_when_it_is_a_checkout(hooks_machine, capsys):
    """The ambiguous case is a git working tree, where the bytes depend on
    whichever branch happens to be out."""
    host = hooks_machine()
    lifecycle.cmd_install(_args(), host=host)
    out = capsys.readouterr().out
    src = lifecycle._pkg_dir()
    is_checkout = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    ).stdout.strip() == "true"
    if not is_checkout:
        pytest.skip("package source is not a git checkout on this machine")
    branch = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert branch in out, (
        f"source is a checkout on '{branch}' and install did not say so; "
        "which branch is installed is exactly what was invisible"
    )


# --------------------------------------------------------------------- utils

class _args:
    harnesses = None
    source = None


def _reported_source(out: str):
    from pathlib import Path
    for line in out.splitlines():
        if line.strip().startswith("source"):
            return Path(line.split(None, 1)[1].split("(")[0].strip())
    raise AssertionError(f"no source line in:\n{out}")


def _find_installed_init(host):
    for p in host.home.rglob("lib/nxthooks/__init__.py"):
        return p
    return None
