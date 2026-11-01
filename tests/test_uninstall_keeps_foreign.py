"""Uninstall removes NxtHooks' own stanzas and nothing else (T1351).

A foreign entry the strip cannot read as a `{"hooks": [...]}` group is not ours
to drop, and a file that held only the operator's content must come back as the
operator wrote it, compact nested objects included.
"""

from __future__ import annotations

import json

from nxthooks import lifecycle

CC_GROUPS = {"session_start": [None], "prompt_submit": [None],
             "before_tool": [None], "session_end": [None]}


def _roundtrip(host, root):
    lifecycle.cc_wire(host, root, CC_GROUPS)
    lifecycle.codex_wire(host, root)
    roots = lifecycle._known_roots(host)
    lifecycle.cc_unwire(host, roots)
    lifecycle.codex_unwire(host, roots)


def test_a_flat_codex_entry_survives_install_then_uninstall(hooks_machine):
    host = hooks_machine("claude-code", "codex")
    foreign = {"hooks": {"PreToolUse": [
        {"command": "echo flat"},
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo nested"}]},
        {"hooks": []},
    ], "Stop": [{"command": "echo stop"}]}}
    host.codex_hooks.write_text(json.dumps(foreign, indent=2) + "\n")

    _roundtrip(host, host.home / "root")

    assert json.loads(host.codex_hooks.read_text()) == foreign


def test_a_claude_shaped_foreign_entry_survives(hooks_machine):
    host = hooks_machine("claude-code", "codex")
    foreign = {"model": "opus", "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]},
        {"command": "echo odd"},
    ]}}
    host.cc_settings.write_text(json.dumps(foreign, indent=2) + "\n")

    _roundtrip(host, host.home / "root")

    assert json.loads(host.cc_settings.read_text()) == foreign


def test_a_file_with_nothing_of_ours_left_is_the_original_text(hooks_machine):
    host = hooks_machine("claude-code", "codex")
    original = (
        '{\n'
        '  "env": {"A": "1", "B": ["x", "y"]},\n'
        '  "permissions": {\n'
        '    "allow": ["Bash(ls)", "Read"]\n'
        '  },\n'
        '  "hooks": {\n'
        '    "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]\n'
        '  }\n'
        '}\n'
    )
    host.cc_settings.write_text(original)
    codex_original = '{\n  "hooks": {\n    "Stop": [{"command": "echo flat"}]\n  }\n}\n'
    host.codex_hooks.write_text(codex_original)

    _roundtrip(host, host.home / "root")

    assert host.cc_settings.read_text() == original
    assert host.codex_hooks.read_text() == codex_original
