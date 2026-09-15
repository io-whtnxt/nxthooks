"""NxtHooks — system-wide agentic hook directory for NxtOS.

One canonical store of hook manifests (~/.config/nxt/hooks/hooks.d/) plus a
single dispatcher that Claude Code, pi, OpenCode, and Hermes adapters call.
Hook behavior is edited once, in its manifest's source location; every
wired harness picks it up on the next event.
"""

__version__ = "0.1.0"

CANONICAL_EVENTS = (
    "session_start",
    "prompt_submit",
    "before_tool",
    "after_tool",
    "stop",
    "session_end",
)
POLICIES = ("enforce", "advise", "off")
FAIL_MODES = ("open", "closed")
HARNESSES = ("claude-code", "codex", "pi", "opencode", "hermes")
