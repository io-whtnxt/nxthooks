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


def test_every_inline_object_in_a_multiline_array_keeps_its_line(hooks_machine):
    host = hooks_machine("claude-code", "codex")
    codex_original = (
        '{\n  "hooks": {\n    "Stop": [{"command": "a"}, {"command": "b"},'
        ' {"command": "c"}],\n    "Other": [\n      {"command": "a"},\n'
        '      {"command": "b"}\n    ]\n  },\n'
        '  "grid": [[1, 2], [3, 4], {"k": [5, 6]}, "s", [7]]\n}\n'
    )
    host.codex_hooks.write_text(codex_original)

    _roundtrip(host, host.home / "root")

    assert host.codex_hooks.read_text() == codex_original


def test_a_foreign_empty_event_list_is_left_alone_and_not_rewritten(hooks_machine):
    host = hooks_machine("claude-code", "codex")
    original = '{\n  "hooks": {\n    "PreToolUse": []\n  }\n}\n'
    host.cc_settings.write_text(original)
    before = host.cc_settings.stat().st_mtime_ns

    lines = lifecycle.cc_unwire(host, None)

    assert lines == []
    assert host.cc_settings.read_text() == original
    assert host.cc_settings.stat().st_mtime_ns == before
