"""What nxthooks may import.

Every hook dispatch imports this package through `run_nxthooks.py`, from the
installed runtime copy, in a subprocess that a sandboxed HOME may have stripped
of its user site-packages. So the package is stdlib-only and imports nothing
but itself; a dependency it does not ship is a ModuleNotFoundError inside a
`fail = "open"` subprocess, which turns the crash into an allow.

(The `nxtoskit` half of this rule, and the import-with-nxtoskit-absent check,
live in `test_nxthooks_standalone.py`.)
"""

import ast
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "nxthooks"

STDLIB = set(sys.stdlib_module_names)


def _all_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.add(node.module)
    return found


def _top(mod: str) -> str:
    return mod.split(".")[0]


def test_nothing_outside_the_standard_library_is_imported_at_module_level():
    violations = {}
    for py in sorted(PKG.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        bad = set()
        for node in tree.body:                 # module level only; a lazy import is a choice
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module]
            bad |= {n for n in names if _top(n) not in STDLIB and _top(n) != "nxthooks"}
        if bad:
            violations[str(py.relative_to(PKG.parent))] = sorted(bad)
    assert violations == {}, violations


def test_the_only_third_party_import_is_a_lazy_optional_one():
    """PyYAML, for Hermes' `config.yaml`, imported inside the function that needs it."""
    violations = {}
    for py in sorted(PKG.rglob("*.py")):
        bad = {m for m in _all_imports(py) if _top(m) not in STDLIB and _top(m) != "nxthooks"}
        if bad:
            violations[str(py.relative_to(PKG.parent))] = sorted(bad)
    assert violations == {"nxthooks/projection/verify.py": ["yaml"]}, violations
