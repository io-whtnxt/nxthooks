"""Installing NxtLint's hook source where the manifest says it lives.

The manifest points at ~/.nxtos/bin/nxtlint-hook.py, outside any working
tree, for the reason every other guard is out of tree: one living inside the
repo it polices is deleted by checking out a branch older than itself, and
then exits 127 on every call while looking perfectly wired.

The shim is a shim, not a copy. A copied hook.py is a second implementation
that drifts from the first in silence; a shim that imports the package can
only ever run the same code the tests ran.
"""

import json
import subprocess
import sys

import pytest

from nxthooks._vendor.nxtlint.install import SHIM_NAME, install_shim

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("ruff") is None, reason="ruff not installed"
)


def test_install_writes_the_shim_the_manifest_names(tmp_path):
    path = install_shim(tmp_path)

    assert path == tmp_path / SHIM_NAME
    assert path.is_file()


def test_install_is_idempotent(tmp_path):
    first = install_shim(tmp_path).read_text(encoding="utf-8")
    second = install_shim(tmp_path).read_text(encoding="utf-8")

    assert first == second


def test_install_creates_a_missing_bin_directory(tmp_path):
    target = tmp_path / "nested" / "bin"

    assert install_shim(target).is_file()


def test_the_installed_shim_actually_lints(tmp_path):
    """The only test that matters: spawn what was installed, exactly as the
    dispatcher would, and see a real finding come back."""
    shim = install_shim(tmp_path / "bin")
    subject = tmp_path / "unused.py"
    subject.write_text("import os\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(shim)],
        input=json.dumps(
            {"event": "after_tool", "tool": "Write",
             "tool_input": {"file_path": str(subject)}}
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert "F401" in proc.stdout
    assert "[NxtLint]" in proc.stdout


def test_the_shim_imports_rather_than_copying(tmp_path):
    """A copy is a second implementation. Guard the property directly: the
    shim must be small enough that it cannot contain the logic."""
    body = install_shim(tmp_path).read_text(encoding="utf-8")

    assert "lint_paths" not in body
    assert len(body.splitlines()) < 20
