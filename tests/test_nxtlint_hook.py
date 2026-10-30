"""NxtLint's NxtHooks source, driven through a real spawn.

Every test runs the actual executable over a real file, because the whole
component is a subprocess contract — envelope in, exit code and CTX lines
out — and a stubbed call would test none of it.

The property under test throughout is that this hook cannot block. It runs on
after_tool, when the write has already landed, and it reports by exiting 0
with CTX lines rather than by exiting 2. That is deliberate: exit 2 would
depend on the manifest's `policy = "advise"` to be downgraded into a note,
and a later operator flipping that field to "enforce" would silently convert
an advisory linter into a gate that refuses an agent's edits.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "nxthooks" / "engines" / "nxtlint" / "hook.py"

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("ruff") is None, reason="ruff not installed"
)


def _run(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=30,
    )


def _write_payload(path, tool="Write") -> dict:
    return {
        "version": 1,
        "harness": "claude-code",
        "event": "after_tool",
        "cwd": str(Path(path).parent),
        "session_id": "test-session",
        "tool": tool,
        "tool_input": {"file_path": str(path)},
    }


def _ctx_lines(proc) -> list[str]:
    return [ln for ln in proc.stdout.splitlines() if ln.startswith("CTX:")]


def test_reports_a_finding_as_a_ctx_line(tmp_path):
    path = tmp_path / "unused.py"
    path.write_text("import os\n\nx = 1\n", encoding="utf-8")

    proc = _run(_write_payload(path))

    assert proc.returncode == 0
    lines = _ctx_lines(proc)
    assert len(lines) == 1
    assert "F401" in lines[0]
    assert "unused.py" in lines[0]


def test_notes_are_branded_like_the_rest_of_the_fleet(tmp_path):
    """NxtGit, NxtGrep, NxtHooks, NxtLint — one capitalisation everywhere the
    operator or the agent actually reads it."""
    path = tmp_path / "unused.py"
    path.write_text("import os\n", encoding="utf-8")

    proc = _run(_write_payload(path))

    assert "[NxtLint]" in proc.stdout


def test_findings_never_block(tmp_path):
    """The load-bearing property: a file full of errors still exits 0."""
    path = tmp_path / "bad.py"
    path.write_text("import os\nimport sys\n\ndef f():\n    return nope\n", encoding="utf-8")

    proc = _run(_write_payload(path))

    assert proc.returncode == 0, "a linter that can block is the wrong design"
    assert _ctx_lines(proc)


def test_clean_file_says_nothing(tmp_path):
    path = tmp_path / "clean.py"
    path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    proc = _run(_write_payload(path))

    assert proc.returncode == 0
    assert _ctx_lines(proc) == []


def test_non_python_file_is_ignored(tmp_path):
    """Write fires on every file type; linting a .md would be pure noise."""
    path = tmp_path / "notes.md"
    path.write_text("import os\n", encoding="utf-8")

    proc = _run(_write_payload(path))

    assert proc.returncode == 0
    assert _ctx_lines(proc) == []


def test_missing_file_is_silent(tmp_path):
    proc = _run(_write_payload(tmp_path / "never-written.py"))

    assert proc.returncode == 0
    assert _ctx_lines(proc) == []


def test_payload_without_a_file_path_is_silent(tmp_path):
    proc = _run(
        {
            "version": 1,
            "event": "after_tool",
            "tool": "Bash",
            "tool_input": {"command": "ls"},
        }
    )

    assert proc.returncode == 0
    assert _ctx_lines(proc) == []


def test_unparseable_stdin_is_silent(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0
    assert _ctx_lines(proc) == []


def test_findings_are_capped_so_one_bad_file_cannot_flood_context(tmp_path):
    """Advice is injected into the agent's context on every single write. An
    unbounded report turns one messy file into a context-budget problem, so
    the cap is the feature, not a limitation."""
    path = tmp_path / "many.py"
    path.write_text("".join(f"import mod{i}\n" for i in range(40)), encoding="utf-8")

    proc = _run(_write_payload(path))

    lines = _ctx_lines(proc)
    assert proc.returncode == 0
    assert len(lines) <= 6, f"expected a capped report, got {len(lines)} lines"
    assert any("40" in ln or "more" in ln.lower() for ln in lines), (
        "a truncated report must say it was truncated"
    )


def test_accepts_the_args_vocabulary_too(tmp_path):
    """normalize() carries both 'tool_input' and 'args'; sources must too."""
    path = tmp_path / "unused.py"
    path.write_text("import os\n", encoding="utf-8")

    proc = _run(
        {
            "version": 1,
            "event": "after_tool",
            "tool": "write",
            "args": {"file_path": str(path)},
        }
    )

    assert proc.returncode == 0
    assert "F401" in "\n".join(_ctx_lines(proc))


# ------------------------------------------------------- codex's apply_patch

def _patch_payload(cwd, body: str) -> dict:
    """Codex's edit tool: one `command` string, no path key anywhere.

    The dispatcher already selects this hook for `apply_patch` -- matchers
    are case-insensitive and `matcher_matches` aliases apply_patch to
    edit/write/multiedit. What it could not do was find the file, so the hook
    read no path, returned 0, and NxtLint was silently inert on every Codex
    edit.
    """
    return {
        "version": 1,
        "harness": "codex",
        "event": "after_tool",
        "cwd": str(cwd),
        "session_id": "test-session",
        "tool": "apply_patch",
        "tool_input": {"command": body},
    }


def test_lints_a_file_an_apply_patch_updated(tmp_path):
    path = tmp_path / "touched.py"
    path.write_text("import os\n", encoding="utf-8")

    proc = _run(_patch_payload(tmp_path, (
        "*** Begin Patch\n"
        "*** Update File: touched.py\n"
        "@@\n"
        "+import os\n"
        "*** End Patch\n"
    )))

    assert proc.returncode == 0
    assert "F401" in "\n".join(_ctx_lines(proc))


def test_lints_a_file_an_apply_patch_added(tmp_path):
    path = tmp_path / "created.py"
    path.write_text("import os\n", encoding="utf-8")

    proc = _run(_patch_payload(tmp_path, (
        "*** Begin Patch\n"
        f"*** Add File: {path}\n"
        "+import os\n"
        "*** End Patch\n"
    )))

    assert proc.returncode == 0
    assert "F401" in "\n".join(_ctx_lines(proc))


def test_a_patch_path_is_resolved_against_the_envelopes_cwd(tmp_path):
    """apply_patch paths are repo-relative; the hook's own cwd is not the
    agent's. Resolving against `os.getcwd()` would silently find nothing."""
    work = tmp_path / "repo"
    (work / "pkg").mkdir(parents=True)
    (work / "pkg" / "mod.py").write_text("import os\n", encoding="utf-8")

    proc = _run(_patch_payload(work, (
        "*** Begin Patch\n"
        "*** Update File: pkg/mod.py\n"
        "*** End Patch\n"
    )))

    assert proc.returncode == 0
    assert "F401" in "\n".join(_ctx_lines(proc))


def test_every_file_in_a_multi_file_patch_is_linted(tmp_path):
    """One apply_patch can touch many files; reporting only the first would
    be a quieter version of the same bug."""
    for name in ("one.py", "two.py"):
        (tmp_path / name).write_text("import os\n", encoding="utf-8")

    proc = _run(_patch_payload(tmp_path, (
        "*** Begin Patch\n"
        "*** Update File: one.py\n"
        "*** Update File: two.py\n"
        "*** End Patch\n"
    )))

    assert proc.returncode == 0
    reported = "\n".join(_ctx_lines(proc))
    assert "one.py" in reported and "two.py" in reported


def test_a_renamed_file_is_linted_at_its_destination(tmp_path):
    """`Move to:` follows `Update File:`; the source path no longer exists,
    so linting it would find nothing and report nothing."""
    (tmp_path / "new.py").write_text("import os\n", encoding="utf-8")

    proc = _run(_patch_payload(tmp_path, (
        "*** Begin Patch\n"
        "*** Update File: old.py\n"
        "*** Move to: new.py\n"
        "*** End Patch\n"
    )))

    assert proc.returncode == 0
    reported = "\n".join(_ctx_lines(proc))
    assert "F401" in reported and "new.py" in reported


def test_a_deleted_file_is_not_linted(tmp_path):
    """The file is written first, and with a real finding in it, so this can
    fail: treating `Delete File:` as a write would report that finding."""
    (tmp_path / "gone.py").write_text("import os\n", encoding="utf-8")

    proc = _run(_patch_payload(tmp_path, (
        "*** Begin Patch\n"
        "*** Delete File: gone.py\n"
        "*** End Patch\n"
    )))

    assert proc.returncode == 0
    assert _ctx_lines(proc) == []


def test_a_patch_touching_no_python_is_silent(tmp_path):
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")

    proc = _run(_patch_payload(tmp_path, (
        "*** Begin Patch\n"
        "*** Update File: README.md\n"
        "*** End Patch\n"
    )))

    assert proc.returncode == 0
    assert _ctx_lines(proc) == []


def test_a_bash_command_that_merely_mentions_a_patch_header_is_ignored(tmp_path):
    """The header is only meaningful inside an apply_patch call. Scanning
    every Bash `command` for it would let an agent's `grep`, `cat` or heredoc
    steer the linter at arbitrary paths."""
    (tmp_path / "bait.py").write_text("import os\n", encoding="utf-8")

    # The header sits at the start of its own line, as it would inside a
    # heredoc -- otherwise the leading `echo ` alone defeats the parser and
    # this proves nothing about the tool-name gate.
    payload = _patch_payload(tmp_path, (
        "cat <<'EOF'\n"
        "*** Begin Patch\n"
        "*** Update File: bait.py\n"
        "*** End Patch\n"
        "EOF\n"
    ))
    payload["tool"] = "Bash"
    payload["tool_name"] = "Bash"

    proc = _run(payload)

    assert proc.returncode == 0
    assert _ctx_lines(proc) == []
