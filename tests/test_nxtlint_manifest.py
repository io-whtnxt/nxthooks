"""NxtLint's manifest ships from the builtin tier, not from a hand-placed file.

A hook whose only copy lives in ~/.config on one machine has no source of
truth and drifts silently — the failure the builtin tier exists to prevent.
These tests pin the fields that carry the design: after_tool (the write has
landed), advise (findings are notes), fail-open (a broken linter must never
cost an agent its edit).
"""

import tomllib

from nxthooks.registry import BUILTIN_DIR
from nxthooks.manifest import _validate


def _entry() -> dict:
    with open(BUILTIN_DIR / "nxtlint.toml", "rb") as fh:
        return tomllib.load(fh)["hook"][0]


def test_nxtlint_is_a_builtin_manifest():
    assert (BUILTIN_DIR / "nxtlint.toml").is_file()


def test_manifest_passes_the_real_validator():
    _validate(_entry(), "nxtlint.toml")


def test_it_runs_after_the_write_has_landed():
    """before_tool would lint a file an agent is mid-way through writing, and
    a half-written function is not a defect worth reporting."""
    assert _entry()["events"] == ["after_tool"]


def test_it_is_advisory_and_fails_open():
    entry = _entry()
    assert entry["policy"] == "advise"
    assert entry["fail"] == "open"


def test_it_only_watches_file_writing_tools():
    assert set(_entry()["matchers"]) == {
        "Write", "Edit", "MultiEdit", "NotebookEdit",
    }
