"""NxtLint's CLI — the blocking half, for a pre-commit hook in any repo.

The hook advises on one file the moment it lands; this gates the whole staged
set at the moment it becomes permanent. Same engine, same pinned rules, but
here a finding is allowed to stop something, because a commit is a coherent
unit and a human is present to act on it.

Deliberately repo-agnostic: it takes paths or reads git's own staged list, so
wiring it is one line in any project's pre-commit rather than a bespoke gate
per repository.
"""

import subprocess
import sys

import pytest

from nxthooks.engines.nxtlint.cli import main

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("ruff") is None, reason="ruff not installed"
)


@pytest.fixture
def repo(tmp_path):
    def run(*args):
        subprocess.run(
            ["git", *args], cwd=tmp_path, check=True,
            capture_output=True, text=True,
        )
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    return tmp_path


def _stage(repo, name, body):
    path = repo / name
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True, capture_output=True)
    return path


def test_clean_paths_exit_zero(tmp_path, capsys):
    path = tmp_path / "clean.py"
    path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    assert main([str(path)]) == 0


def test_a_finding_exits_nonzero(tmp_path, capsys):
    path = tmp_path / "bad.py"
    path.write_text("import os\n", encoding="utf-8")

    assert main([str(path)]) == 1
    assert "F401" in capsys.readouterr().out


def test_staged_lints_only_what_is_staged(repo, capsys):
    _stage(repo, "staged.py", "import os\n")
    (repo / "unstaged.py").write_text("import sys\n", encoding="utf-8")

    code = main(["--staged", "--cwd", str(repo)])

    out = capsys.readouterr().out
    assert code == 1
    assert "staged.py" in out
    assert "unstaged.py" not in out


def test_staged_ignores_non_python_files(repo, capsys):
    _stage(repo, "notes.md", "import os\n")

    assert main(["--staged", "--cwd", str(repo)]) == 0


def test_staged_on_a_clean_index_exits_zero(repo):
    _stage(repo, "fine.py", "def f():\n    return 1\n")

    assert main(["--staged", "--cwd", str(repo)]) == 0


def test_staged_outside_a_repo_is_not_a_crash(tmp_path, capsys):
    """A pre-commit hook that explodes outside git blocks the commit for a
    reason that has nothing to do with the code being committed."""
    assert main(["--staged", "--cwd", str(tmp_path)]) == 0


def test_deleted_files_are_not_linted(repo):
    _stage(repo, "gone.py", "import os\n")
    subprocess.run(["git", "commit", "-qm", "add"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "rm", "-q", "gone.py"], cwd=repo, check=True,
                   capture_output=True)

    assert main(["--staged", "--cwd", str(repo)]) == 0


def test_module_is_runnable_as_a_script(tmp_path):
    """Pre-commit hooks invoke it as a command, not an import."""
    path = tmp_path / "bad.py"
    path.write_text("import os\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "nxthooks.engines.nxtlint.cli", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )

    assert proc.returncode == 1
    assert "F401" in proc.stdout
