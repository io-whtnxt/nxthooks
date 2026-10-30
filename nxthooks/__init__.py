"""NxtHooks — system-wide agentic hook directory for NxtOS.

One canonical store of hook manifests (~/.config/nxt/hooks/hooks.d/) plus a
single dispatcher that Claude Code, pi, OpenCode, and Hermes adapters call.
Hook behavior is edited once, in its manifest's source location; every
wired harness picks it up on the next event.
"""

__version__ = "0.2.0"

CANONICAL_EVENTS = (
    "session_start",
    "subagent_start",
    "prompt_submit",
    "before_tool",
    "after_tool",
    "stop",
    "session_end",
)
POLICIES = ("enforce", "advise", "off")
FAIL_MODES = ("open", "closed")
HARNESSES = ("claude-code", "codex", "pi", "opencode", "hermes", "muse", "pool")

#: Harnesses `nxthooks install`/`uninstall` may auto-detect and wire.
#: Muse dispatches correctly -- its blocking contract is verified against
#: Muse Code 1.3.0 -- but nobody has driven a real write through its
#: settings.json writer to record what it tolerates, the way
#: nxthooks.projection.detect's table requires for every other harness
#: here. Until that verification happens, muse stays reachable by
#: dispatch but excluded from the automated install/uninstall lifecycle.
INSTALLABLE_HARNESSES = tuple(h for h in HARNESSES if h not in {"muse", "pool"})

# Pool has no verified hook transport. CLI start/before/after/end is the
# explicit fallback; including its identity never installs invented hooks.
