"""The NxtPack native provider is a process boundary, so drive it as one."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nxthooks import HARNESSES, POLICIES
from nxthooks import pack

PACK_PATH = Path(pack.__file__)
VALID = {"policy": {"default": "advise"}, "scope": {"include": ["~/src/*"], "exclude": []}}


def run_provider(payload, argv=None, cwd=None):
    """Invoke the provider the way NxtPack does: absolute argv, JSON on stdin."""
    argv = argv or [sys.executable, str(PACK_PATH)]
    completed = subprocess.run(argv, input=json.dumps(payload), capture_output=True,
                               text=True, timeout=15, cwd=cwd)
    return completed.returncode, json.loads(completed.stdout)


def test_describe_over_stdin_returns_the_three_editable_paths():
    code, response = run_provider({"action": "describe", "document": VALID})
    assert code == 0 and response["ok"] is True
    assert [field["path"] for field in response["fields"]] == [
        ["policy", "default"], ["scope", "include"], ["scope", "exclude"]
    ]


def test_module_invocation_matches_script_invocation():
    by_path = run_provider({"action": "describe", "document": VALID})
    by_module = run_provider({"action": "describe", "document": VALID},
                             argv=[sys.executable, "-m", "nxthooks.pack"])
    assert by_path == by_module


def test_harness_flags_are_never_offered_as_edits():
    fields = pack.editable_fields({"harnesses": {name: True for name in HARNESSES}})
    assert all(field["path"][0] != "harnesses" for field in fields)


def test_absent_field_is_marked_not_present_and_carries_a_suggestion():
    (policy, include, exclude) = pack.editable_fields({"policy": {"default": "off"}})
    assert policy["present"] is True and policy["suggested"] == "off"
    assert include["present"] is False and include["suggested"] == []
    assert exclude["present"] is False and exclude["suggested"] == []


def test_policy_field_declares_every_supported_policy_as_a_choice():
    (policy, _, _) = pack.editable_fields(VALID)
    assert policy["enum"] == list(POLICIES)


def test_scope_fields_declare_a_collection_template():
    (_, include, exclude) = pack.editable_fields(VALID)
    for field in (include, exclude):
        assert field["collection"] == "array" and field["item_template"] == ""


def test_invalid_policy_is_rejected_with_its_reason_and_a_failing_exit_code():
    code, response = run_provider({"action": "validate", "document": {"policy": {"default": "nope"}}})
    assert code == 1 and response["ok"] is False
    assert "Invalid NxtHooks policy" in response["error"]


def test_validate_does_not_describe_fields():
    _, response = run_provider({"action": "validate", "document": VALID})
    assert response["valid"] is True and "fields" not in response


@pytest.mark.parametrize("request_body", [
    {"action": "install", "document": VALID},
    {"action": "describe"},
    {"action": "describe", "document": VALID, "target": "mini"},
    ["describe", VALID],
])
def test_unsupported_requests_are_refused(request_body):
    with pytest.raises((ValueError, KeyError, TypeError)):
        pack.handle(request_body)


def test_malformed_stdin_fails_closed():
    completed = subprocess.run([sys.executable, str(PACK_PATH)], input="{not json",
                               capture_output=True, text=True, timeout=15)
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["ok"] is False


def test_revision_tracks_the_declared_schema(monkeypatch):
    before = pack.revision()
    monkeypatch.setitem(pack.CONFIG_SCHEMA, "title", "changed")
    assert pack.revision() != before


REVISION_SOURCES = ("__init__.py", "config.py", "pack.py")


def test_every_source_behind_the_schema_is_named_in_the_revision():
    assert set(pack.REVISION_SOURCES) == set(REVISION_SOURCES)


def test_provider_validates_with_the_copy_it_was_invoked_from(tmp_path):
    """An absolute-path invocation must load that tree's validator, not an installed one."""
    staged = _stage_package(tmp_path)
    (staged / "config.py").write_text(
        (staged / "config.py").read_text()
        + '\n\ndef validate_config(document):\n    raise ValueError("staged-copy-marker")\n')
    code, response = run_provider({"action": "validate", "document": VALID},
                                  argv=[sys.executable, str(staged / "pack.py")],
                                  cwd=tmp_path)
    assert code == 1 and "staged-copy-marker" in response["error"]


@pytest.mark.parametrize("source", REVISION_SOURCES)
def test_editing_any_named_source_invalidates_the_revision(tmp_path, source):
    staged = _stage_package(tmp_path)
    (staged / source).write_bytes((staged / source).read_bytes() + b"\n# edited\n")
    _, response = run_provider({"action": "validate", "document": VALID},
                               argv=[sys.executable, str(staged / "pack.py")],
                               cwd=tmp_path)
    assert response["revision"] != pack.revision()


def _stage_package(tmp_path):
    staged = tmp_path / "nxthooks"
    shutil.copytree(PACK_PATH.parent, staged,
                    ignore=shutil.ignore_patterns("__pycache__"))
    return staged
