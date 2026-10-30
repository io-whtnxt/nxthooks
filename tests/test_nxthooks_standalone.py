"""NxtHooks imports nothing from nxtoskit.

The dependency direction is `nxtoskit -> nxthooks` only. This is what lets the
package be cut into its own repo: a stranger who installs NxtHooks alone never
had a `nxtoskit` to import.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "nxthooks"


def _python_files():
    return [p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts]


def _imports_nxtoskit(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):  # walk, not iter_child_nodes: function-level imports count
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "nxtoskit" for a in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] == "nxtoskit":
                lines.append(node.lineno)
    return lines


def test_no_module_in_the_package_imports_nxtoskit():
    leaks = {
        f"{path.relative_to(REPO)}:{line}"
        for path in _python_files()
        for line in _imports_nxtoskit(ast.parse(path.read_text(encoding="utf-8")))
    }
    assert not leaks, sorted(leaks)


def test_no_manifest_or_adapter_names_a_nxtoskit_module():
    """A builtin manifest's `provision` is a module name run with -m; a string is
    an import as far as the runtime is concerned."""
    offenders = [
        str(path.relative_to(REPO))
        for path in PACKAGE.rglob("*")
        if path.is_file() and path.suffix in {".toml", ".sh", ".yaml", ".json", ".ts"}
        and "nxtoskit" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, offenders


@pytest.mark.parametrize("module", [
    "nxthooks.cli", "nxthooks.lifecycle", "nxthooks.dispatch", "nxthooks.pack",
    "nxthooks.engines.nxtlint.hook", "nxthooks.engines.nxtgrep.hook",
    "nxthooks.engines.snapshots.hook",
])
def test_imports_with_nxtoskit_unavailable(module):
    code = (
        "import sys; sys.modules['nxtoskit'] = None\n"
        f"import importlib; importlib.import_module({module!r})\n"
    )
    proc = subprocess.run([sys.executable, "-P", "-c", code], cwd=REPO, env={"PYTHONPATH": str(REPO), "PATH": ""},
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_a_clean_home_install_never_touches_dot_nxtos(hooks_machine):
    from nxthooks import lifecycle

    host = hooks_machine("claude-code")
    args = type("A", (), {"harnesses": None, "source": None})()
    assert lifecycle.cmd_install(args, host) == 0
    assert not (host.home / ".nxtos").exists()
    assert not (host.home / "Library" / "Application Support" / "NxtOS").exists()


def test_no_adapter_sends_the_operator_to_a_command_this_package_does_not_ship():
    """Every remedy an adapter prints must be runnable on a machine that has
    NxtHooks and nothing else."""
    offenders = [str(p.relative_to(REPO)) for p in (PACKAGE / "adapters").rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts
                 and "nxtos doctor" in p.read_text(encoding="utf-8")]
    assert offenders == []
