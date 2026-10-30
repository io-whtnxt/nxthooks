"""NxtLint's engine: running ruff over real files and parsing real output.

Every test here drives the real ruff binary over a real file on disk. A
stubbed ruff would prove nothing about the only thing this module does —
translate one tool's JSON into findings — and the parse is exactly where a
version bump breaks us.
"""

import pytest

from nxthooks.engines.nxtlint.engine import lint_paths

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("ruff") is None, reason="ruff not installed"
)


def _py(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_unused_import_is_reported_with_its_rule_code(tmp_path):
    path = _py(tmp_path, "unused.py", "import os\n\nx = 1\n")

    findings, error = lint_paths([path])

    assert error is None
    assert [f.code for f in findings] == ["F401"]
    assert findings[0].line == 1
    assert "os" in findings[0].message


def test_undefined_name_is_reported(tmp_path):
    """F821 is the rule this whole component exists for: an undefined name in
    a guard is what turns it into a silent no-op that still looks wired."""
    path = _py(tmp_path, "guard.py", "def check():\n    return verdict\n")

    findings, error = lint_paths([path])

    assert error is None
    assert "F821" in [f.code for f in findings]


def test_clean_file_yields_no_findings(tmp_path):
    path = _py(tmp_path, "clean.py", "def add(a, b):\n    return a + b\n")

    assert lint_paths([path]) == ([], None)


def test_syntax_error_is_reported_rather_than_swallowed(tmp_path):
    path = _py(tmp_path, "broken.py", "def oops(:\n    pass\n")

    findings, error = lint_paths([path])

    assert error is None
    assert "invalid-syntax" in [f.code for f in findings]


def test_style_rules_are_not_reported(tmp_path):
    """The rule set is pinned, not inherited from whatever ruff defaults to
    this month. 0.16 turned PL/RUF/EXE on by default, which would spend an
    agent's context on `subprocess.run` missing an explicit `check=` — true,
    stylistic, and not why anyone wired a linter into their agent loop."""
    path = _py(
        tmp_path,
        "stylish.py",
        "import subprocess\n\n\ndef go():\n    return subprocess.run(['ls'])\n",
    )

    findings, error = lint_paths([path])

    assert error is None
    assert findings == [], f"expected silence on style, got {findings}"


def test_the_pinned_set_survives_a_repo_with_its_own_ruff_config(tmp_path):
    """Ruff discovers config from the target file's directory. A linted repo
    that selects every rule must not turn our advisory note into a firehose."""
    (tmp_path / "ruff.toml").write_text('lint.select = ["ALL"]\n', encoding="utf-8")
    path = _py(
        tmp_path,
        "stylish.py",
        "import subprocess\n\n\ndef go():\n    return subprocess.run(['ls'])\n",
    )

    findings, error = lint_paths([path])

    assert error is None
    assert [f.code for f in findings] == []


def test_no_paths_is_not_a_ruff_invocation(tmp_path):
    assert lint_paths([]) == ([], None)


def test_missing_ruff_is_silent_not_an_error(tmp_path):
    """Advisory means a machine without the binary says nothing at all,
    rather than narrating its own absence on every single file write."""
    path = _py(tmp_path, "unused.py", "import os\n")

    assert lint_paths([path], ruff="ruff-that-is-not-installed") == ([], None)
