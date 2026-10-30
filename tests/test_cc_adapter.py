"""Claude Code stanza wiring, on a simulated machine rather than the
operator's own `~/.claude/settings.json`.

Mirrors tests/test_codex_adapter.py's coverage of the same shared
strip/converge logic, applied to the cc_wire/cc_unwire side instead of the
Codex side.
"""

from __future__ import annotations

import json

import pytest

from nxthooks import lifecycle
from nxthooks.projection import NotDetected, parse_command_marker


ALL_CC_GROUPS = {
    "session_start": [None],
    "prompt_submit": [None],
    "before_tool": [None],
    "session_end": [None],
}


def _commands(path):
    doc = json.loads(path.read_text())
    return [
        hook["command"]
        for groups in doc["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]


def _managed(path):
    return [c for c in _commands(path) if lifecycle.owned_command(c, None)]


def test_cc_wire_projects_matcher_groups_by_event(hooks_machine):
    host = hooks_machine("claude-code")
    groups = {
        "session_start": [],
        "prompt_submit": [None],
        "before_tool": ["Bash", "Write"],
        "after_tool": ["Edit"],
        "session_end": [],
    }

    lifecycle.cc_wire(host, host.home / "runtime", groups)
    data = json.loads(host.cc_settings.read_text())

    assert set(data["hooks"]) == {"UserPromptSubmit", "PreToolUse", "PostToolUse"}
    assert [group.get("matcher") for group in data["hooks"]["PreToolUse"]] == [
        "Bash", "Write",
    ]
    assert data["hooks"]["PostToolUse"][0].get("matcher") == "Edit"
    assert len(_managed(host.cc_settings)) == 4


def test_cc_wire_is_idempotent_and_preserves_foreign_hooks(hooks_machine):
    host = hooks_machine("claude-code")
    foreign = {"type": "command", "command": "python3 foreign.py"}
    host.cc_settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [foreign]}]}}))

    root = host.home / "runtime"
    lifecycle.cc_wire(host, root, ALL_CC_GROUPS)
    lifecycle.cc_wire(host, root, ALL_CC_GROUPS)
    data = json.loads(host.cc_settings.read_text())

    # SessionEnd joined the plan so a session-scoped resource (the persistent
    # kernel) has somewhere to be cleaned up. Stop here is the operator's own
    # foreign hook, not ours: `stop` fires every assistant turn, not at close.
    assert set(data["hooks"]) == {
        "SessionStart", "UserPromptSubmit", "PreToolUse", "SessionEnd", "Stop",
    }
    assert len(_managed(host.cc_settings)) == 4
    assert foreign in data["hooks"]["Stop"][0]["hooks"]

    lifecycle.cc_unwire(host, (root,))
    assert json.loads(host.cc_settings.read_text()) == {
        "hooks": {"Stop": [{"hooks": [foreign]}]}
    }


def test_every_wired_command_carries_a_marker_covering_it(hooks_machine):
    """The marker's sha12 is over the command with the marker removed. A
    writer that hashed the whole stanza would produce a `verify` reporting
    `altered` for every slot forever, which reads exactly like drift."""
    host = hooks_machine("claude-code")
    lifecycle.cc_wire(host, host.home / "runtime", ALL_CC_GROUPS)

    for command in _commands(host.cc_settings):
        marker = parse_command_marker(command)
        assert marker is not None
        assert marker.component == "hooks"
        assert marker.covers(lifecycle.command_payload(command))


def test_cc_wire_preserves_a_marker_owned_by_another_install(hooks_machine):
    host = hooks_machine("claude-code")
    other = {
        "type": "command",
        "command": f'{lifecycle.MARKER} sh "/other/runtime/bin/cc-dispatch.sh" before_tool',
    }
    host.cc_settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [other]}]}})
    )

    root = host.home / "runtime"
    lifecycle.cc_wire(host, root, ALL_CC_GROUPS)
    lifecycle.cc_unwire(host, (root,))

    data = json.loads(host.cc_settings.read_text())
    assert other in data["hooks"]["PreToolUse"][0]["hooks"]
    assert _managed(host.cc_settings) == [other["command"]]


def test_cc_wire_reconciles_a_previous_install_root_from_the_ledger(hooks_machine):
    """The `previous_roots` argument this replaced had to be threaded in by
    the caller. The ledger already records the dispatcher each stanza points
    at, so a re-wire from a moved root reconciles itself."""
    host = hooks_machine("claude-code")
    old_root = host.home / "old-runtime"
    new_root = host.home / "new-runtime"

    lifecycle.cc_wire(host, old_root, ALL_CC_GROUPS)
    lifecycle.cc_wire(host, new_root, ALL_CC_GROUPS)

    commands = _commands(host.cc_settings)
    assert len(commands) == 4
    assert all(str(new_root) in command for command in commands)
    assert all(str(old_root) not in command for command in commands)


def test_cc_wire_refuses_a_machine_with_no_claude_code(hooks_machine):
    """The mandatory property, at the level below `cmd_install`: the writer
    itself will not run without a positive detection, so wiring an absent
    harness cannot be reintroduced by a caller that forgets to check."""
    host = hooks_machine()

    with pytest.raises(NotDetected):
        lifecycle.cc_wire(host, host.home / "runtime", ALL_CC_GROUPS)

    assert not (host.home / ".claude").exists()


def test_cc_wire_preserves_indent_and_non_ascii(hooks_machine):
    """`json.dumps(data, indent=2)` defaults to `ensure_ascii=True`, so every
    wire used to rewrite a stranger's accented text into escapes and move the
    foreign remainder under them."""
    host = hooks_machine("claude-code")
    host.cc_settings.write_text(
        json.dumps({"statusLine": {"command": "café ~/bin/ligne"}}, indent=4,
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    lifecycle.cc_wire(host, host.home / "runtime", ALL_CC_GROUPS)
    text = host.cc_settings.read_text(encoding="utf-8")

    assert "café" in text
    assert "\\u00e9" not in text
    assert '\n    "hooks"' in text


def test_a_settings_file_nxtos_authored_is_recorded_and_taken_back(hooks_machine):
    """Detection can be positive on the binary alone, so a machine with
    Claude Code but no settings file gets one written. Leaving `{}` behind
    afterwards leaves a file the harness never had.

    Recorded at the wire, removed at the prune, and only while it is still
    empty: the record is the licence, emptiness is the guard.
    """
    host = lifecycle.Host(home=hooks_machine().home, env={},
                          path_lookup=lambda name: "/usr/bin/claude")
    root = host.home / "runtime"

    lifecycle.cc_wire(host, root, ALL_CC_GROUPS)
    assert host.cc_settings.exists()
    assert host.cc_settings in lifecycle._read_created(host)

    lifecycle.cc_unwire(host, (root,))
    assert lifecycle.prune_created(host, [host.cc_settings]) == [host.cc_settings]
    assert not host.cc_settings.exists()


def test_a_settings_file_the_operator_has_since_used_is_kept(hooks_machine):
    host = lifecycle.Host(home=hooks_machine().home, env={},
                          path_lookup=lambda name: "/usr/bin/claude")
    root = host.home / "runtime"

    lifecycle.cc_wire(host, root, ALL_CC_GROUPS)
    lifecycle.cc_unwire(host, (root,))
    host.cc_settings.write_text(json.dumps({"model": "opus"}))

    assert lifecycle.prune_created(host, [host.cc_settings]) == []
    assert host.cc_settings.exists()


def test_a_hook_disabled_for_claude_code_does_not_widen_its_stanza(hooks_machine):
    host = hooks_machine("claude-code")
    host.hooks_d.mkdir(parents=True, exist_ok=True)
    (host.hooks_d / "guards.toml").write_text(
        '[[hook]]\nname = "bash-guard"\nevents = ["before_tool"]\nmatchers = ["Bash"]\n'
        'source = "/bin/true"\n\n'
        '[[hook]]\nname = "pi-only-preflight"\nevents = ["before_tool"]\n'
        'source = "/bin/true"\n[hook.harnesses.claude-code]\nenabled = false\n',
        encoding="utf-8",
    )

    before_tool = lifecycle.wiring_groups(host)["before_tool"]
    assert "Bash" in before_tool
    assert None not in before_tool
    assert lifecycle.matcher_groups_for("before_tool", host, harness="pi") == [None]
