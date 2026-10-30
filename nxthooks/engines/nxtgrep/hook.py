#!/usr/bin/env python3
"""NxtGrep as a NxtHooks source: scan a tool call, refuse on a hit.

Replaces two things doing the same job in different dialects. The NxtOS
in-process wrapper (NxtOS's `hooks/builtin/nxtgrep/check.py`) spoke
`{decision, reason}` JSON and bootstrapped with `parents[4]`; the Claude Code
adapter (`adapters/nxtgrep/claude-code-hook.py`) spoke exit codes and shelled
out to `~/.nxtos/bin/nxtgrep`. Each carried its own envelope translation, and
the pattern pack lived in neither package -- which is the reason nxtgrep could
not be installed from a wheel at all.

The dispatcher already normalises every harness's envelope, so one
harness-agnostic source is enough:

    exit 0   allow; anything on stderr is still surfaced by the adapters
    exit 2   block; stderr is the reason shown to the model

Fail-closed in every direction the runtime already treats as refusal:
unparseable input, unusable config, a malformed pack line, a payload too large
to scan completely. Two deliberate exceptions, both carried over from check.py
rather than invented here: `policy = "off"` skips scanning entirely including
the error paths, and zero loaded rules continues, because a gate with nothing
to enforce should not brick opted-in agents.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


#: The top-level package this module ships inside: the directory the walk
#: below looks for to find the root it puts on `sys.path`.
PACKAGE_MARKER = "nxthooks"


def package_root(start: Path | None = None) -> Path:
    """The directory `nxthooks` is importable from, found by marker.

    Same reasoning as nxtlint's: the dispatcher may spawn this with no
    guarantee that nxthooks is on sys.path, and a miscounted root resolves to
    a real directory that merely lacks the package -- so the import fails
    inside a subprocess nobody reads, under a policy that turns the crash into
    an allow. Raising is the point.
    """
    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        if (parent / PACKAGE_MARKER / "__init__.py").is_file():
            return parent
    raise RuntimeError(
        f"no directory above {here} contains {PACKAGE_MARKER}/__init__.py; "
        "nxtgrep cannot locate the package it belongs to"
    )


sys.path.insert(0, str(package_root()))

from nxthooks.engines.nxtgrep.config import load_config  # noqa: E402
from nxthooks.engines.nxtgrep.engine import Pack, load_pack, scan  # noqa: E402

#: Shipped with the package, not with the NxtOS hook tree. That split is what
#: kept nxtgrep out of a wheel: the pack was read from a directory no
#: distribution carried.
BUILTIN_PACK = Path(__file__).resolve().parent / "patterns" / "default.conf"

SKIP_DEFAULTS_ENV = "NXTGREP_SKIP_DEFAULTS"

ALLOW, BLOCK = 0, 2


def _block(reason: str) -> int:
    print(reason, file=sys.stderr)
    return BLOCK


def _load_packs(config) -> tuple[list[Pack], list[str], str | None]:
    """Resolve config into loaded packs, or an error reason.

    Sources are echoed back so a refusal can say which pack spoke: the
    operator deciding whether to fix a rule or fight it needs that without
    re-reading config.
    """
    packs: list[Pack] = []
    sources: list[str] = []

    if config.builtin_enabled:
        pack, error = load_pack(BUILTIN_PACK)
        if error:
            return [], [], error
        if pack is not None and pack.rules:
            packs.append(pack)
            sources.append("builtin")

    for path in config.pack_files():
        pack, error = load_pack(path)
        if error:
            return [], [], error
        if pack is not None and pack.rules:
            packs.append(pack)
            sources.append(str(path))

    return packs, sources, None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError) as exc:
        return _block(f"NxtGrep could not parse its input payload: {exc}")
    if not isinstance(payload, dict):
        return _block("NxtGrep: hook payload was not an object")

    tool_name = payload.get("tool_name", "?")
    tool_input = payload.get("tool_input")

    try:
        config = load_config()
    except ValueError as exc:
        return _block(f"NxtGrep: {exc}")

    if config.policy == "off":
        # An operator decision, never an accident: off skips scanning
        # entirely, including the fail-closed paths below.
        return ALLOW

    # Env override beats the config file: it exists for tests, and for an
    # operator who needs one command to bypass the builtin pack without
    # editing installation data mid-incident.
    if os.environ.get(SKIP_DEFAULTS_ENV) == "1":
        config = type(config)(
            builtin_enabled=False,
            user_patterns_file=config.user_patterns_file,
            extra_files=config.extra_files,
            max_bytes=config.max_bytes,
            max_reported_hits=config.max_reported_hits,
        )

    packs, sources, error = _load_packs(config)
    if error:
        return _block(f"NxtGrep: {error}")
    if not packs:
        return ALLOW

    hits, truncated = scan(
        tool_input,
        packs,
        max_bytes=config.max_bytes,
        max_reported_hits=config.max_reported_hits,
    )

    detail = "; ".join(f"{h.rule_source} at {h.location}: …{h.snippet}…" for h in hits)

    if (hits or truncated) and config.policy == "advise":
        print(f"[nxtgrep] advise: pattern hit(s) in {tool_name}. {detail}",
              file=sys.stderr)
        return ALLOW

    if hits:
        more = " (report truncated)" if truncated else ""
        return _block(
            f"NxtGrep refused {tool_name}: pattern hit(s){more}. "
            f"Packs: {', '.join(sources)}. Hits: {detail}"
        )

    if truncated:
        return _block(
            f"NxtGrep refused {tool_name}: payload exceeds {config.max_bytes} "
            "bytes and cannot be scanned completely"
        )

    return ALLOW


if __name__ == "__main__":
    sys.exit(main())
