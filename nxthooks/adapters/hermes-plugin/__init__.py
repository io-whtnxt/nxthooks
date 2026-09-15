"""nxthooks transport for Hermes.

Thin by contract (2026-08-22 component-config spec): selection, policy,
timeouts, and execution all live in the NxtHooks dispatcher. This translates
Hermes's events onto the canonical vocabulary and applies verdicts at the
maximum capability Hermes actually grants:

  pre_llm_call   -> prompt_submit  inject. Returned context is merged into
                                   the CURRENT turn's user message (never the
                                   system prompt, which would break the cache
                                   prefix). Hermes does not read this hook's
                                   return as a veto, so a block verdict
                                   degrades to an in-context warning -- a
                                   declared gap, never a pretend-block.
  pre_tool_call  -> before_tool    real pre-execution block. A block verdict
                                   returns {"action": "block"}; Hermes vetoes
                                   the call and the message becomes the tool
                                   result the model sees.
  post_tool_call -> after_tool     observe. Hermes discards the return.

  session_start                    registered as a system prompt section,
                                   because on_session_start's return value is
                                   discarded and using it to inject would be
                                   a silent no-op. The section's callable is
                                   re-evaluated for each new session build.

Unlike the pi and OpenCode transports, a Hermes plugin is Python loaded into
the agent process rather than an external command, so this calls the shared
dispatcher directly with no shell layer in between.

Installed to ~/.hermes/plugins/nxthooks/ by `nxthooks install`. Enable per
profile via `plugins.enabled` in that profile's config.yaml -- plugins are
opt-in, so installing alone activates nothing.

Behavior changes belong in the dispatcher and the hook sources, not here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

ROOT = Path(os.environ.get("NXTHOOKS_ROOT") or Path.home() / ".local/share/nxt/hooks")
RUNNER = ROOT / "lib" / "run_nxthooks.py"
TIMEOUT_SECONDS = 20


#: What `_dispatch` returns when there is no runner to call, as distinct from
#: the `None` it returns for every other failure. The distinction is the whole
#: point: a dispatcher that ran and failed is a bug to fix, and a dispatcher
#: that is not installed is a session running with no guards at all, which the
#: operator has to be told about.
RUNNER_MISSING: dict = {"unavailable": True}


def _dispatch(event: str, payload: dict) -> Optional[dict]:
    """Run the shared dispatcher. Returns None on any failure.

    Fail-open is mandatory here, not stylistic: Hermes isolates a raising
    plugin, but a transport that propagated dispatcher trouble as a block
    would stop real work over its own plumbing.
    """
    if not RUNNER.is_file():
        return RUNNER_MISSING
    try:
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "dispatch",
             "--harness", "hermes", "--event", event, "--out", "json"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        outcome = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return outcome if isinstance(outcome, dict) else None


#: Said once per process, not once per turn. A missing runner still fails
#: open at `pre_tool_call` -- Hermes vetoing every tool because a dispatcher
#: moved would be worse than the gap -- but silence about a security layer
#: that is not running is its own defect, so the two injection surfaces say
#: so on the first turn that notices.
_runner_notice_shown = False


def _runner_missing_note() -> str:
    global _runner_notice_shown
    if _runner_notice_shown:
        return ""
    _runner_notice_shown = True
    return (f"[nxthooks] runner missing at {RUNNER}; hooks are NOT running "
            "this session. Run: nxtos doctor")


def _context_text(outcome: dict) -> str:
    parts = [c.strip() for c in (outcome.get("context") or []) if c and c.strip()]
    if outcome.get("verdict") == "block" and outcome.get("reason"):
        parts.append(f"[nxthooks] {outcome['reason']}")
    return "\n\n".join(parts).strip()


def on_pre_llm_call(**kwargs: Any) -> Optional[dict]:
    outcome = _dispatch("prompt_submit", {
        "cwd": os.getcwd(),
        "session_id": kwargs.get("session_id") or "",
        "prompt": str(kwargs.get("user_message") or "")[:8000],
    })
    if outcome is RUNNER_MISSING:
        note = _runner_missing_note()
        return {"context": note} if note else None
    if not outcome:
        return None
    text = _context_text(outcome)
    return {"context": text} if text else None


def on_pre_tool_call(**kwargs: Any) -> Optional[dict]:
    outcome = _dispatch("before_tool", {
        "cwd": os.getcwd(),
        "session_id": kwargs.get("session_id") or "",
        "tool_name": kwargs.get("tool_name") or "",
        "tool_input": kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {},
    })
    if not outcome or outcome is RUNNER_MISSING:
        return None
    if outcome.get("verdict") == "block":
        return {"action": "block",
                "message": outcome.get("reason") or "blocked by nxthooks"}
    return None


def on_post_tool_call(**kwargs: Any) -> None:
    _dispatch("after_tool", {
        "cwd": os.getcwd(),
        "session_id": kwargs.get("session_id") or "",
        "tool_name": kwargs.get("tool_name") or "",
        "tool_input": kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {},
    })
    return None


def _session_section(session_info: Mapping[str, Any]) -> str:
    outcome = _dispatch("session_start", {
        "cwd": os.getcwd(),
        "session_id": str(session_info.get("session_id") or ""),
    })
    if outcome is RUNNER_MISSING:
        return _runner_missing_note()
    return _context_text(outcome) if outcome else ""


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_system_prompt_section("nxthooks.session", _session_section)
