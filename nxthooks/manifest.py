"""Hook manifest loading — the NxtHooks directory.

Each hooks.d/*.toml file declares one or more [[hook]] entries:

    [[hook]]
    name    = "claims-guard"
    events  = ["before_tool"]
    source  = "~/.nxtos/bin/claims-guard.sh"
    matchers = ["Bash"]            # tool-name globs; empty = all tools
    policy  = "enforce"            # enforce | advise | off
    fail    = "closed"             # open | closed
    timeout_ms = 5000
    enabled = true
    pi_preflight = true            # before_tool only: safe to run with no
                                    # tool/args yet, so pi can dispatch it at
                                    # turn start instead of degrading to warn

    [hook.harnesses.pi]
    enabled = false                # opt this harness out of this hook

`source` is the point of the whole system: a configurable location, usually
outside any working repo, holding the one canonical implementation.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import CANONICAL_EVENTS, FAIL_MODES, POLICIES

from .platform import hooks_d as _hooks_d


def hooks_d_default(
    env: dict | None = None,
    home: Path | str | None = None,
) -> Path:
    """Resolved per call, never cached at import.

    A module-level constant here read the config home as it stood when the
    module first loaded, which is correct in a one-shot subprocess and wrong
    in anything long-lived -- the installer, the desktop app, a test suite.
    """
    return _hooks_d(env, home)


class ManifestError(ValueError):
    pass


def _validate(entry: dict, origin: str) -> dict:
    name = entry.get("name")
    if not name or not isinstance(name, str):
        raise ManifestError(f"{origin}: [[hook]] missing string 'name'")
    events = entry.get("events")
    if not events or not isinstance(events, list):
        raise ManifestError(f"{origin}/{name}: 'events' must be a non-empty list")
    for event in events:
        if event not in CANONICAL_EVENTS:
            raise ManifestError(
                f"{origin}/{name}: unknown event '{event}' (known: {', '.join(CANONICAL_EVENTS)})"
            )
    source = entry.get("source")
    if not source or not isinstance(source, str):
        raise ManifestError(f"{origin}/{name}: 'source' must be a path")
    if entry.get("policy", "enforce") not in POLICIES:
        raise ManifestError(f"{origin}/{name}: policy must be one of {POLICIES}")
    if entry.get("fail", "open") not in FAIL_MODES:
        raise ManifestError(f"{origin}/{name}: fail must be one of {FAIL_MODES}")
    timeout = entry.get("timeout_ms")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0):
        raise ManifestError(f"{origin}/{name}: 'timeout_ms' must be a positive integer")
    if "pi_preflight" in entry:
        if not isinstance(entry["pi_preflight"], bool):
            raise ManifestError(f"{origin}/{name}: 'pi_preflight' must be a bool")
        if entry["pi_preflight"] and "before_tool" not in events:
            raise ManifestError(f"{origin}/{name}: 'pi_preflight' only applies to a hook with 'before_tool' in events")
    return entry


def load_manifests(
    hooks_d: Path | None = None,
    *,
    library: Path | None = None,
    env: dict | None = None,
    home: Path | str | None = None,
) -> list[dict]:
    """Every hook that applies, as the resolved builtin < library < user union.

    Lazy import: registry.py imports from this module at module scope, so
    importing it back at module scope here would be circular. Inside the
    function body both modules have finished loading.

    `hooks_d` still scopes the USER tier only -- a Host-scoped caller points
    it at a sandbox, and the builtin and library tiers come from the package
    and the install root as they always do.
    """
    from .registry import resolve

    return resolve(
        library=library,
        hooks_d=hooks_d,
        env=env,
        home=home,
    )[0]


def source_path(entry: dict) -> Path:
    return Path(os.path.expanduser(entry["source"]))


def harness_enabled(entry: dict, harness: str) -> bool:
    override = (entry.get("harnesses") or {}).get(harness, {})
    if isinstance(override, dict):
        return bool(override.get("enabled", True))
    return bool(override)


#: Each harness's own name for a tool, against the Claude-Code-shaped names a
#: manifest is written in. Normalising here is the point of the component:
#: "write a hook once and run it on five harnesses" is false the moment a
#: manifest has to know that Hermes calls bash `terminal`.
#:
#: Every row observed in a live turn, never read from documentation. Hermes'
#: two cost it every matcher-bearing hook in the catalogue -- the secret
#: scanner included -- because `matchers = ["Bash"]` and
#: `matchers = ["Write", ...]` select nothing against `terminal` and
#: `write_file`, and selecting nothing is indistinguishable from running and
#: finding nothing.
#:
#: Casing is not handled here; `matcher_matches` lowercases both sides, which
#: is what makes OpenCode's and pi's `bash` match a manifest's `Bash`.
NATIVE_TOOL_ALIASES = {
    # Codex puts every edit through one unified patch tool.
    "apply_patch": {"edit", "write", "multiedit"},
    # Hermes (hermes-agent 0.21.1).
    "terminal": {"bash"},
    "write_file": {"write"},
    "edit_file": {"edit"},
}


def matcher_matches(entry: dict, tool: str | None) -> bool:
    """Tool-name glob, case-insensitive: harnesses disagree on casing
    (Claude Code 'Write' vs OpenCode 'write'), manifests shouldn't care."""
    import fnmatch

    matchers = entry.get("matchers") or []
    if not matchers:
        return True
    if not tool:
        return False
    # Codex reports its unified patch tool as ``apply_patch`` while its hook
    # matcher layer aliases that call to Claude-style Edit and Write. Preserve
    # the same equivalence inside the shared dispatcher, otherwise Codex would
    # invoke us and the canonical file-edit guards would then select nothing.
    aliases = {tool.lower()} | NATIVE_TOOL_ALIASES.get(tool.lower(), set())
    return any(
        fnmatch.fnmatch(alias, matcher.lower())
        for alias in aliases
        for matcher in matchers
    )
