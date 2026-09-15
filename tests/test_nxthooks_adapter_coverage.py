"""Every harness adapter dispatches every canonical event, or says why not.

A hook wired to an event its harness never dispatches is invisible: it
installs, it verifies, it lists, and it never runs. That shipped twice --
Claude Code's PostToolUse and OpenCode's tool.execute.after -- so the
coverage is asserted here rather than left to be noticed.

The check is two-part, not a single whole-file string search: the
harness-side registration key (the object key OpenCode returns, the string
pi passes to `pi.on(...)`) must be present, AND the canonical event string
must appear inside *that* handler's own balanced-brace body -- not merely
somewhere in the file. A bare `event: "x"` search is foolable by a comment
mentioning the event, or by an event dispatched under the wrong key; scoping
the search to the registration's own block closes that hole while staying
well short of a real TypeScript parser.
"""

import re
from pathlib import Path

import pytest

ADAPTERS = Path(__file__).resolve().parents[1] / "nxthooks" / "adapters"
OPENCODE = ADAPTERS / "opencode-plugin" / "nxthooks.ts"
PI = ADAPTERS / "pi-extension" / "index.ts"

# Events a harness genuinely cannot provide, with the reason. An entry here is
# a documented capability limit, not a gap.
#
# pi used to be listed here for before_tool, on the belief that it has no
# pre-tool interception point. It has one: `tool_call` is documented "Fired
# before a tool executes. Can block." and its handler returns
# `{block, reason, terminate}` (pi-coding-agent
# dist/core/extensions/types.d.ts, ToolCallEvent / ToolCallEventResult).
# The exemption was asserted, never checked, and it cost pi every blocking
# hook in the catalogue.
EXEMPT: dict[tuple[str, str], str] = {}

# The harness-side registration key that wires up each (harness, event) pair.
# OpenCode registers by object key on the returned plugin; pi registers by
# the string literal it passes to `pi.on(...)`.
#
# pi's three tool events are not interchangeable, and picking the wrong one is
# silent: `tool_execution_end` carries only
# `{type, toolCallId, toolName, result, isError}` -- no tool arguments at all
# (observed live, 2026-09-15). A hook selected off that event can never see
# what it is meant to inspect.
REGISTRATIONS = {
    ("opencode", "before_tool"): '"tool.execute.before"',
    ("opencode", "after_tool"): '"tool.execute.after"',
    ("pi", "before_tool"): '"tool_call"',
    ("pi", "after_tool"): '"tool_result"',
}

# The field each harness puts the *tool name* in. Reading the wrong one is not
# an error, it is `undefined` -> "" -> `matcher_matches` returns False for
# every matcher-bearing hook, so NxtLint, NxtGrep and the nxtgit guards all
# install, verify, list, and never run. pi shipped that way: the adapter read
# `event?.tool ?? event?.name`, and pi populates neither -- it uses `toolName`.
TOOL_NAME_SOURCE = {
    ("opencode", "before_tool"): "input?.tool",
    ("opencode", "after_tool"): "input?.tool",
    ("pi", "before_tool"): "event?.toolName",
    ("pi", "after_tool"): "event?.toolName",
}

# The handler argument each harness actually populates with the tool's args,
# and — where the harness has a second argument that looks equally plausible —
# the one it does not. OpenCode's two tool events disagree: `before` carries
# args on `output`, `after` on `input` (@opencode-ai/plugin dist/index.d.ts
# lines 231-254). Reading the wrong one is not an error, it is `undefined`,
# so the hook is selected, spawned, and silently no-ops on an empty payload.
#
# pi's entries were derived from the spec rather than from pi: both said
# `event?.args`, which exists on none of its tool events. `tool_call` and
# `tool_result` both carry the arguments on `input`.
ARGS_SOURCE = {
    ("opencode", "before_tool"): "output?.args",
    ("opencode", "after_tool"): "input?.args",
    ("pi", "before_tool"): "event?.input",
    ("pi", "after_tool"): "event?.input",
}
ARGS_NOT_FROM = {
    ("opencode", "before_tool"): "input?.args",
    ("opencode", "after_tool"): "output?.args",
    ("pi", "before_tool"): "event?.args",
    ("pi", "after_tool"): "event?.args",
}


def _dispatched(source: str) -> set[str]:
    return set(re.findall(r'event:\s*"(\w+)"', source))


def _handler_body(source: str, registration: str) -> str | None:
    """The balanced-brace block opening at `registration`'s first `{` after
    it, or None if `registration` never appears in `source`.

    Brace-counting, not a parser: assumes the registration key is followed
    by a `(...) => { ... }` handler (true of both adapters today) and that no
    `{`/`}` inside the handler sits inside a string or comment (also true of
    both files today).
    """
    idx = source.find(registration)
    if idx == -1:
        return None
    start = source.find("{", idx)
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(source[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    return None


def _assert_wired(harness: str, event: str, path: Path):
    if (harness, event) in EXEMPT:
        return
    registration = REGISTRATIONS[(harness, event)]
    body = _handler_body(path.read_text(), registration)
    assert body is not None, f"{harness} never registers {registration}"
    assert event in _dispatched(body), (
        f"{harness} registers {registration} but its handler body never "
        f'dispatches event: "{event}"'
    )


def _assert_args_source(harness: str, event: str, path: Path):
    if (harness, event) in EXEMPT:
        return
    registration = REGISTRATIONS[(harness, event)]
    body = _handler_body(path.read_text(), registration)
    assert body is not None, f"{harness} never registers {registration}"
    right, wrong = ARGS_SOURCE[(harness, event)], ARGS_NOT_FROM[(harness, event)]
    assert right in body, (
        f"{harness} {registration} must read the tool args from `{right}`; "
        f"its harness populates nothing else"
    )
    assert wrong not in body, (
        f"{harness} {registration} reads `{wrong}`, which is always undefined "
        f"on this event — the dispatch would carry an empty args payload"
    )


def _assert_tool_name_source(harness: str, event: str, path: Path):
    if (harness, event) in EXEMPT:
        return
    registration = REGISTRATIONS[(harness, event)]
    body = _handler_body(path.read_text(), registration)
    assert body is not None, f"{harness} never registers {registration}"
    right = TOOL_NAME_SOURCE[(harness, event)]
    assert right in body, (
        f"{harness} {registration} must read the tool name from `{right}`; "
        f"reading anything else yields \"\", and an empty tool name makes "
        f"matcher_matches() return False for every matcher-bearing hook"
    )


def test_every_ts_adapter_reads_the_tool_name_from_the_field_its_harness_populates():
    """The failure that hid all the others.

    An empty tool name is not an error anywhere in the chain: the adapter
    builds a valid envelope, the dispatcher accepts it, `matcher_matches`
    returns False for every hook that declares matchers, and the dispatch
    reports success having selected nothing. Observed live on pi 2026-09-15:
    `tool: ""` on both tool events, for a call whose real `toolName` was
    "bash".
    """
    for harness, path in (("opencode", OPENCODE), ("pi", PI)):
        for event in ("before_tool", "after_tool"):
            _assert_tool_name_source(harness, event, path)


def test_pi_uses_its_real_pre_tool_blocking_point():
    """pi can block before execution; the adapter must use that, not a
    post-hoc advisory on an event that fires once the tool has already run."""
    body = _handler_body(PI.read_text(), '"tool_call"')
    assert body is not None, (
        'pi never registers "tool_call" -- its documented pre-tool hook '
        "(\"Fired before a tool executes. Can block.\")"
    )
    assert "block" in body, (
        'pi registers "tool_call" but never returns a block verdict, so a '
        "blocking hook still cannot stop a tool call"
    )


def test_opencode_after_tool_reads_args_from_input_not_output():
    """The after-hook was copied from the before-hook, which carries args on
    `output`; on `after` that is always undefined. Registration and event name
    both looked correct the whole time, which is why this pins the field."""
    _assert_args_source("opencode", "after_tool", OPENCODE)


def test_every_ts_adapter_reads_args_from_the_field_its_harness_populates():
    for harness, path in (("opencode", OPENCODE), ("pi", PI)):
        for event in ("before_tool", "after_tool"):
            _assert_args_source(harness, event, path)


def test_opencode_dispatches_after_tool():
    _assert_wired("opencode", "after_tool", OPENCODE)


def test_pi_dispatches_after_tool():
    _assert_wired("pi", "after_tool", PI)


def test_every_ts_adapter_covers_the_tool_events():
    for harness, path in (("opencode", OPENCODE), ("pi", PI)):
        for event in ("before_tool", "after_tool"):
            _assert_wired(harness, event, path)


# ------------------------------------------------- harness-native tool names

#: What each harness calls the tool, against the canonical name a manifest
#: writes. Every row observed in a live turn, not read from documentation.
NATIVE_TOOL_NAMES = [
    # (harness, native name, a canonical matcher that must select it)
    ("codex", "apply_patch", "Edit"),
    ("hermes", "terminal", "Bash"),
    ("hermes", "write_file", "Write"),
    ("opencode", "bash", "Bash"),
    ("pi", "bash", "Bash"),
]


@pytest.mark.parametrize("harness,native,matcher", NATIVE_TOOL_NAMES)
def test_a_manifest_matcher_selects_each_harnesss_own_tool_name(harness, native, matcher):
    """"Write a hook once and run it on five harnesses" is the whole claim,
    and it is false the moment a manifest has to know what each one calls
    bash.

    Hermes calls it `terminal` and its writer `write_file`, so
    `matchers = ["Bash"]` and `matchers = ["Write", ...]` selected nothing
    there -- every matcher-bearing hook in the catalogue, the secret scanner
    included, installed and verified and never ran. Observed in a live turn
    on hermes-agent 0.21.1, the same way Codex's `apply_patch` was found.
    """
    from nxthooks.manifest import matcher_matches

    assert matcher_matches({"matchers": [matcher]}, native), (
        f"{harness} calls it {native!r}; a manifest matching {matcher!r} "
        "does not select it"
    )


def test_an_unrelated_tool_is_still_not_selected():
    """The aliases must not turn a matcher into a wildcard."""
    from nxthooks.manifest import matcher_matches

    assert not matcher_matches({"matchers": ["Bash"]}, "write_file")
    assert not matcher_matches({"matchers": ["Write"]}, "terminal")
    assert not matcher_matches({"matchers": ["Bash"]}, "read_file")
