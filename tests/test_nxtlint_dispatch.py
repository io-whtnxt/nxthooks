"""NxtLint through the real NxtHooks dispatcher, end to end.

Unit tests prove the hook speaks the contract. This proves the dispatcher
agrees: that the seeded manifest selects on the right event and matchers,
that exit-0-with-CTX becomes a context injection, and above all that a file
full of findings still comes back `allow`.

The last one is the property the whole design rests on, and it is the one no
unit test can establish — "the hook exits 0" and "the dispatcher therefore
lets the edit through" are two different claims.
"""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from nxthooks.registry import BUILTIN_DIR
from nxthooks.engines.nxtlint.install import install_shim

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("ruff") is None, reason="ruff not installed"
)


@pytest.fixture
def wired(tmp_path):
    """A hooks.d holding only nxtlint, pointed at a freshly installed shim."""
    shim = install_shim(tmp_path / "bin")
    hooks_d = tmp_path / "config" / "nxt" / "hooks" / "hooks.d"
    hooks_d.mkdir(parents=True)
    body = (BUILTIN_DIR / "nxtlint.toml").read_text(encoding="utf-8").replace(
        '"~/.local/share/nxt/lint/bin/nxtlint-hook.py"', json.dumps(str(shim))
    )
    (hooks_d / "nxtlint.toml").write_text(body, encoding="utf-8")
    assert tomllib.loads(body)["hook"][0]["source"] == str(shim)
    # Every other builtin is switched off by name, or the dispatch runs this
    # machine's own copy of it.
    for builtin in BUILTIN_DIR.glob("*.toml"):
        if builtin.name != "nxtlint.toml":
            off = builtin.read_text(encoding="utf-8").replace("[[hook]]\n", "[[hook]]\nenabled = false\n")
            (hooks_d / builtin.name).write_text(off, encoding="utf-8")
    return tmp_path


def _dispatch(wired, envelope: dict, event: str = "after_tool") -> dict:
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(wired / "config")
    env["XDG_DATA_HOME"] = str(wired / "data")
    env["XDG_STATE_HOME"] = str(wired / "state")
    env["XDG_CACHE_HOME"] = str(wired / "cache")
    proc = subprocess.run(
        [sys.executable, "-m", "nxthooks", "dispatch",
         "--harness", "claude-code", "--event", event, "--out", "json"],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, f"dispatch failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _envelope(path) -> dict:
    return {
        "cwd": str(Path(path).parent),
        "session_id": "dispatch-test",
        "tool_name": "Write",
        "tool_input": {"file_path": str(path)},
    }


def test_a_finding_arrives_as_context_and_the_edit_is_allowed(wired, tmp_path):
    subject = tmp_path / "unused.py"
    subject.write_text("import os\n", encoding="utf-8")

    out = _dispatch(wired, _envelope(subject))

    assert out["verdict"] == "allow"
    context = "\n".join(out["context"])
    assert "[NxtLint]" in context
    assert "F401" in context


def test_a_badly_broken_file_still_allows_the_edit(wired, tmp_path):
    """The property the design rests on. Findings are advice, never a gate."""
    subject = tmp_path / "bad.py"
    subject.write_text(
        "import os\nimport sys\n\ndef f():\n    return nope\n", encoding="utf-8"
    )

    out = _dispatch(wired, _envelope(subject))

    assert out["verdict"] == "allow"
    assert out["context"], "findings should still have been reported"


def test_a_clean_file_injects_nothing(wired, tmp_path):
    subject = tmp_path / "clean.py"
    subject.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    out = _dispatch(wired, _envelope(subject))

    assert out["verdict"] == "allow"
    assert out["context"] == []


def test_it_does_not_fire_on_a_non_matching_tool(wired, tmp_path):
    subject = tmp_path / "unused.py"
    subject.write_text("import os\n", encoding="utf-8")
    envelope = _envelope(subject) | {"tool_name": "Bash"}

    out = _dispatch(wired, envelope)

    assert out["verdict"] == "allow"
    assert out["context"] == []


def test_it_does_not_fire_before_the_tool_runs(wired, tmp_path):
    """Selecting on after_tool is what keeps it off half-written files."""
    subject = tmp_path / "unused.py"
    subject.write_text("import os\n", encoding="utf-8")

    out = _dispatch(wired, _envelope(subject), event="before_tool")

    assert out["context"] == []


def test_the_wired_dispatch_runs_nxtlint_and_nothing_of_the_machines(wired, tmp_path):
    """Builtins always load and name `~/...` sources, so without switching them
    off these tests ran the developer's real secret-scan and snapshot-save and
    wrote failures to the real ledger; a stranger machine without them failed."""
    subject = tmp_path / "unused.py"
    subject.write_text("import os\n", encoding="utf-8")

    after = _dispatch(wired, _envelope(subject))
    before = _dispatch(wired, _envelope(subject), event="before_tool")

    assert [hook["hook"] for hook in after["ran"]] == ["nxtlint"]
    assert before["ran"] == [] and before["unavailable"] == []
