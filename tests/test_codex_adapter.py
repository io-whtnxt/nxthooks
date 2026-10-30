"""Codex transport contract and disposable-repository safety proofs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nxthooks import HARNESSES
from nxthooks.cli import build_parser
from nxthooks.config import DEFAULTS
from nxthooks import capability, lifecycle
from nxthooks.dispatch import aggregate, cmd_dispatch, emit_native_codex, normalize, run_hook
from nxthooks.manifest import matcher_matches
from nxthooks.projection import NotDetected


def _entry(name: str, source: Path, event: str, **extra) -> dict:
    return {
        "name": name,
        "events": [event],
        "source": str(source),
        "policy": "enforce",
        "fail": "closed",
        **extra,
    }


def test_muse_dispatch_routes_through_the_claude_code_native_emitter(monkeypatch, capsys):
    """Muse Code declares the same settings.json hooks shape as Claude Code
    (SessionStart/UserPromptSubmit/PreToolUse, identical nesting) and its
    blocking contract -- exit 2, stderr as the reason -- was verified
    against Muse Code 1.3.0 (see muse-dispatch.sh). cmd_dispatch reuses
    emit_native_cc for it rather than the generic json-dump fallback, which
    the other unregistered-native harnesses (pi, opencode, hermes) still
    get. With no hooks configured, emit_native_cc prints nothing on allow;
    the generic fallback would print the outcome JSON -- that's the
    observable difference this test pins."""
    import argparse
    import io

    from nxthooks import dispatch

    monkeypatch.setattr(dispatch, "load_manifests", lambda: [])
    monkeypatch.setattr(dispatch, "load_config", lambda cwd=None: {})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": "", "prompt": "hi"})))

    args = argparse.Namespace(
        harness="muse", event="prompt_submit", event_positional="",
        out="native", preflight=False,
    )
    assert cmd_dispatch(args) == 0
    assert capsys.readouterr().out == ""


def test_muse_dispatches_but_is_not_offered_to_the_unverified_installer():
    dispatch = build_parser().parse_args([
        "dispatch", "--harness", "muse", "--event", "prompt_submit",
    ])
    assert dispatch.harness == "muse"

    with pytest.raises(SystemExit) as refused:
        build_parser().parse_args(["install", "--harnesses", "muse"])
    assert refused.value.code == 2

def test_capability_matrix_names_all_harnesses_and_honest_gaps():
    assert HARNESSES == ("claude-code", "codex", "pi", "opencode", "hermes", "muse", "pool")
    for harness in HARNESSES:
        assert harness in lifecycle.CAPABILITY_MATRIX
    assert capability.CELLS["before_tool block"][1] == "block*"
    assert "Manual fallback" in lifecycle.CAPABILITY_MATRIX
    assert all(row[-1] == "unsupported; CLI" for row in capability.CELLS.values())


def test_the_config_never_enables_a_transport_on_its_own():
    """The `[harnesses]` block is a record of what install detected, not an
    instruction. Every default is false so a config-less machine claims
    nothing it has not been observed to have."""
    assert set(DEFAULTS["harnesses"]) == set(HARNESSES)
    assert not any(DEFAULTS["harnesses"].values())


def test_codex_wiring_matches_the_verified_capability_fixture():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "codex-hook-capabilities.json").read_text()
    )
    assert fixture["verified_codex_cli"] == "0.151.0"
    assert fixture["project_hooks_require_trust"] is True
    assert {
        event: detail["canonical"] for event, detail in fixture["events"].items()
    } == {native: canonical for canonical, native in lifecycle.EVENT_TO_CODEX.items()}
    assert fixture["events"]["PreToolUse"]["effect"] == "block"
    assert fixture["pre_tool_exclusions"]


def _commands(path):
    doc = json.loads(path.read_text())
    return [
        hook["command"]
        for groups in doc["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]


def test_codex_wire_is_idempotent_and_preserves_foreign_hooks(hooks_machine):
    host = hooks_machine("codex")
    foreign = {"type": "command", "command": "python3 foreign.py"}
    host.codex_hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [foreign]}]}}))
    root = host.home / "runtime"

    lifecycle.codex_wire(host, root)
    lifecycle.codex_wire(host, root)
    data = json.loads(host.codex_hooks.read_text())

    assert set(data["hooks"]) == {
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "Stop", "SessionEnd",
    }
    managed = [c for c in _commands(host.codex_hooks)
               if lifecycle.owned_command(c, None)]
    assert len(managed) == 6
    assert data["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 3
    assert foreign in data["hooks"]["Stop"][0]["hooks"]

    lifecycle.codex_unwire(host, (root,))
    assert json.loads(host.codex_hooks.read_text()) == {
        "hooks": {"Stop": [{"hooks": [foreign]}]}
    }


def test_codex_wire_refuses_to_overwrite_malformed_foreign_config(hooks_machine):
    host = hooks_machine("codex")
    malformed = '{"hooks": '
    host.codex_hooks.write_text(malformed)

    with pytest.raises(lifecycle.CodexHooksError, match="refusing to overwrite"):
        lifecycle.codex_wire(host, host.home / "runtime")
    assert host.codex_hooks.read_text() == malformed


@pytest.mark.parametrize("payload", [
    [],
    {"hooks": []},
    {"hooks": {"Stop": {}}},
    {"hooks": {"Stop": [{"hooks": {}}]}},
])
def test_codex_wire_refuses_valid_json_with_unsafe_shape(hooks_machine, payload):
    host = hooks_machine("codex")
    original = json.dumps(payload)
    host.codex_hooks.write_text(original)

    with pytest.raises(lifecycle.CodexHooksError, match="refusing to overwrite"):
        lifecycle.codex_wire(host, host.home / "runtime")
    assert host.codex_hooks.read_text() == original


def test_codex_wire_preserves_marker_owned_by_another_install(hooks_machine):
    host = hooks_machine("codex")
    other = {
        "type": "command",
        "command": f'{lifecycle.MARKER} sh "/other/runtime/bin/codex-dispatch.sh" stop',
    }
    host.codex_hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [other]}]}}))

    root = host.home / "runtime"
    lifecycle.codex_wire(host, root)
    lifecycle.codex_unwire(host, (root,))

    assert json.loads(host.codex_hooks.read_text()) == {
        "hooks": {"Stop": [{"hooks": [other]}]}
    }


def test_codex_wire_reconciles_a_previous_install_root_from_the_ledger(hooks_machine):
    host = hooks_machine("codex")
    old_root = host.home / "old-runtime"
    new_root = host.home / "new-runtime"

    lifecycle.codex_wire(host, old_root)
    lifecycle.codex_wire(host, new_root)
    commands = _commands(host.codex_hooks)

    assert len(commands) == 6
    assert all(str(new_root) in command for command in commands)
    assert all(str(old_root) not in command for command in commands)


def test_codex_wire_refuses_a_machine_with_no_codex(hooks_machine):
    host = hooks_machine("claude-code")

    with pytest.raises(NotDetected):
        lifecycle.codex_wire(host, host.home / "runtime")

    assert not (host.home / ".codex").exists()


def test_installed_codex_transport_calls_shared_dispatcher(hooks_machine):
    host = hooks_machine()
    root = lifecycle.install_runtime(
        host, {"install": {"root": str(host.home / "runtime")}}
    )
    transport = root / "bin" / "codex-dispatch.sh"
    assert transport.is_file()
    text = transport.read_text()
    assert "--harness codex --out native" in text
    assert "run_nxthooks.py" in text


def test_apply_patch_selects_existing_file_edit_manifests():
    entry = {"matchers": ["Write", "Edit", "NotebookEdit"]}
    assert matcher_matches(entry, "apply_patch") is True
    assert matcher_matches({"matchers": ["Bash"]}, "apply_patch") is False


def test_muse_write_file_and_edit_file_select_file_edit_manifests():
    entry = {"matchers": ["Write", "Edit", "MultiEdit", "NotebookEdit"]}
    assert matcher_matches(entry, "write_file") is True
    assert matcher_matches(entry, "edit_file") is True
    assert matcher_matches(entry, "Write") is True
    assert matcher_matches({"matchers": ["Bash"]}, "write_file") is False
    assert matcher_matches({"matchers": ["Write"]}, "bash") is False


def test_codex_native_outputs_are_event_specific(capsys):
    allow = {
        "verdict": "allow", "reason": "", "context": ["route advice"],
        "ran": [{
            "hook": "route", "policy": "enforce", "fail": "open",
            "verdict": "allow", "reason": "", "context": ["route advice"],
            "error": "", "ms": 1,
        }],
    }
    for event, native in [
        ("session_start", "SessionStart"),
        ("prompt_submit", "UserPromptSubmit"),
        ("before_tool", "PreToolUse"),
        ("after_tool", "PostToolUse"),
    ]:
        assert emit_native_codex(event, allow) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["hookSpecificOutput"] == {
            "hookEventName": native,
            "additionalContext": "route advice",
        }

    assert emit_native_codex("stop", allow) == 0
    assert json.loads(capsys.readouterr().out) == {"systemMessage": "route advice"}
    assert emit_native_codex("session_end", allow) == 0
    assert capsys.readouterr().out == ""

    blocked = {**allow, "verdict": "block", "reason": "claimed tree"}
    assert emit_native_codex("before_tool", blocked) == 2
    assert "claimed tree" in capsys.readouterr().err


def test_codex_context_is_deduped_and_bounded(capsys, monkeypatch):
    monkeypatch.setenv("NXTHOOKS_CONTEXT_MAX_CHARS", "256")
    outcome = aggregate([
        {
            "hook": "warning", "policy": "advise", "fail": "open",
            "verdict": "block", "reason": "review this action", "context": [],
            "error": "", "ms": 1,
        },
        {
            "hook": "noise-one", "policy": "enforce", "fail": "open",
            "verdict": "allow", "reason": "", "context": ["x" * 600],
            "error": "", "ms": 1,
        },
        {
            "hook": "noise-two", "policy": "enforce", "fail": "open",
            "verdict": "allow", "reason": "", "context": ["x" * 600],
            "error": "", "ms": 1,
        },
    ])

    assert outcome["context_chars"] <= 256
    assert emit_native_codex("prompt_submit", outcome) == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("[advise] review this action")
    assert context.count("[nxthooks] additional advisory context omitted") == 1
    assert len(context) <= 256


def test_disposable_repo_context_route_and_claim_guard_are_non_mutating(tmp_path, capsys):
    """Observation is read-only and the claimed-tree edit is vetoed pre-tool."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    sentinel = repo / "sentinel.txt"
    sentinel.write_text("original\n")
    claim = repo / ".claimed"
    claim.write_text("actor-2\n")

    context = tmp_path / "git_context.py"
    context.write_text(
        "import json,subprocess,sys\n"
        "p=json.load(sys.stdin)\n"
        "b=subprocess.run(['git','-C',p['cwd'],'branch','--show-current'],"
        "capture_output=True,text=True).stdout.strip() or '(unborn)'\n"
        "print('CTX: git branch '+b)\n"
    )
    route = tmp_path / "route.py"
    route.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin)\n"
        "print('CTX: Route advice for '+p.get('prompt',''))\n"
    )
    guard = tmp_path / "guard.py"
    guard.write_text(
        "import json,pathlib,sys\n"
        "p=json.load(sys.stdin)\n"
        "if (pathlib.Path(p['cwd'])/'.claimed').exists():\n"
        " print('nxtgit: claimed tree', file=sys.stderr); raise SystemExit(2)\n"
    )

    def status() -> tuple[str, list[str], str]:
        porcelain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1"],
            check=True, capture_output=True, text=True,
        ).stdout
        return porcelain, sorted(p.name for p in repo.iterdir()), sentinel.read_text()

    before = status()
    session = run_hook(
        _entry("git-context", context, "session_start"),
        normalize("codex", "session_start", {"cwd": str(repo), "session_id": "s1"}),
    )
    assert "git branch" in session["context"][0]

    routed = run_hook(
        _entry("route", route, "prompt_submit"),
        normalize("codex", "prompt_submit", {
            "cwd": str(repo), "session_id": "s1", "prompt": "work on claimed tree",
        }),
    )
    outcome = aggregate([routed])
    assert emit_native_codex("prompt_submit", outcome) == 0
    assert "Route advice" in json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]

    envelope = normalize("codex", "before_tool", {
        "cwd": str(repo), "session_id": "s1", "tool_name": "apply_patch",
        "tool_input": {"command": "*** Begin Patch\n*** Update File: sentinel.txt"},
    })
    assert matcher_matches({"matchers": ["Write"]}, envelope["tool"])
    denied = aggregate([run_hook(
        _entry("claimed-tree-guard", guard, "before_tool", matchers=["Write"]),
        envelope,
    )])
    assert emit_native_codex("before_tool", denied) == 2
    assert "claimed tree" in capsys.readouterr().err
    assert status() == before
