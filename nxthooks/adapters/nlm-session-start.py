#!/usr/bin/env python3
"""Emulate NLM's once-per-session pointer block for harnesses without one.

OpenCode's NxtHooks adapter has no session_start event (declared gap, see
the README capability matrix), so this binds to prompt_submit
instead and fires only on the first prompt of a session -- the same
once-per-session-via-prompt_submit technique nxtgit-route uses
on every harness.

Muse Code has a real SessionStart event, but NLM ships no `nlm connect
muse` target, so there is no native NLM wiring for it either; this hook
covers muse the same way, on first prompt_submit. The runtime is read from
the dispatcher envelope (`harness`), not hardcoded.

Claude Code, pi, and Hermes get NLM's real session-start hook through their
own native wiring (`nlm connect <harness>`) and are opted out of this hook
via harness overrides in its manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATE_DIR = Path.home() / ".local" / "state" / "nxthooks" / "nlm-session-start"
NLM_ENV = Path.home() / ".nlm" / ".env"
RECALL_URL = "http://127.0.0.1:3940/api/recall"
RECALL_LIMIT = 5
TIMEOUT_S = 4.0


def marker_path(session_id: str) -> Path:
    """Return a fixed-width marker path without trusting session_id as a filename."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return STATE_DIR / f"{digest}.fired"


def claim_session(session_id: str) -> bool:
    """Atomically claim a session, failing closed for recall but open for the hook."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        marker_path(session_id).touch(exist_ok=False)
    except FileExistsError:
        return False
    except OSError:
        return False
    return True


def load_token() -> str:
    token = os.environ.get("NLM_MCP_TOKEN")
    if token:
        return token
    try:
        for line in NLM_ENV.read_text().splitlines():
            if line.startswith("NLM_MCP_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def truncate_summary(text: str, max_len: int = 200) -> str:
    if len(text) <= max_len:
        return text
    window = text[:max_len]
    for i in range(len(window) - 1, 59, -1):
        if window[i] in ".!?" and (i + 1 == len(window) or window[i + 1] == " "):
            return window[: i + 1]
    for i in range(len(window) - 1, 59, -1):
        if window[i] == " ":
            return window[:i] + " ..."
    return window + " ..."


def format_pointer_block(hits: list[dict]) -> list[str]:
    if not hits:
        return []
    lines = ["## Possibly-relevant prior sessions (nlm-memory)"]
    for h in hits:
        date_part = (h.get("startedAt") or "")[:10]
        summary = h.get("summary")
        if summary:
            lines.append(f"- {h['id']} · {h['label']} ({date_part}) — {truncate_summary(summary)}")
        else:
            lines.append(f"- {h['id']} · {h['label']} ({date_part})")
    lines.append(
        "NLM tools: recall_sessions (search), get_session (full transcript), "
        "recall_facts (prior decisions), get_fact_history (how a decision evolved)."
    )
    return lines


def main() -> int:
    try:
        envelope = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    session_id = envelope.get("session_id") or ""
    prompt = (envelope.get("prompt") or "").strip()
    if not session_id or not prompt:
        return 0

    if not claim_session(session_id):
        return 0
    # Claiming before the network call means a slow or failed recall cannot
    # re-fire on the next prompt and duplicate the block mid-session.

    token = load_token()
    query = urllib.parse.urlencode(
        {
            "q": prompt[:2000],
            "mode": "keyword",
            "limit": RECALL_LIMIT,
            "withFacts": "false",
            "withExemplars": "false",
        }
    )
    req = urllib.request.Request(f"{RECALL_URL}?{query}")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    req.add_header("x-recall-source", "session-start")
    req.add_header("x-recall-runtime", str(envelope.get("harness") or "unknown"))

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return 0  # fail-open: a missed pointer block is never worth blocking the turn

    for line in format_pointer_block(body.get("results") or []):
        print(f"CTX: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
