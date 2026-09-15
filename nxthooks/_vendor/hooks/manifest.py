"""Parse and validate one hook manifest. Never raises.

A broken manifest yields a disabled record carrying a reason, so the hooks
surface can show the hook greyed out with an explanation. An operator who
installed a hook and cannot find it in the listing has no way to debug it;
silent omission is the failure mode this exists to prevent.

Phase names, scope values and the timeout floor are enforced here rather than
in the JSON schema. The runtime needs PHASES as a constant regardless, so
keeping the enum in Python leaves one authority for it, and a rejection can
name the values that would have been accepted instead of quoting a schema
fragment at an operator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import jsonschema
import yaml

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema" / "nxtos-hook.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

PHASES = ("pre_tool_call", "post_tool_call", "transform_tool_result", "on_session_end")
SCOPES = ("agent", "workspace")

_DEFAULT_TIMEOUT_MS = 5000
_DEFAULT_SCOPE = "agent"


@dataclass(frozen=True)
class HookRecord:
    id: str
    display_name: str
    version: str
    phases: dict[str, str]
    timeout_ms: int
    scope: str
    source: str
    data: dict = field(default_factory=dict)
    enabled: bool = False
    reason: str = ""


def _disabled(hook_id: str, reason: str) -> HookRecord:
    id_str = str(hook_id)
    return HookRecord(
        id=id_str, display_name=id_str, version="", phases={}, timeout_ms=0,
        scope="", source="", enabled=False, reason=reason,
    )


def _resolve_phases(declared: dict, hook_dir: Path) -> tuple[dict[str, str], str]:
    """Absolute phase paths, confined to the hook directory.

    Confinement is checked after symlink resolution and before existence,
    because `../../../bin/sh` names a file that does exist: an existence check
    alone would let a manifest hand the runtime an arbitrary binary to execute.

    `is_file`, not `exists`: a directory exists, and a manifest naming one would
    load enabled and then pay a failed subprocess on every tool call. A manifest
    error belongs at discovery, reported once with a reason.

    `.resolve()` itself raises `ValueError` for a string carrying an embedded
    NUL byte (valid YAML, e.g. a phase path of `"bin/ bad.sh"` with a `\x00`),
    which without a guard would propagate straight out of `load_manifest`
    rather than degrade to a reason like every other broken declaration here.
    """
    root = hook_dir.resolve()
    phases: dict[str, str] = {}
    for phase, rel in declared.items():
        if phase not in PHASES:
            return {}, f"unknown phase {phase!r}; valid phases: {', '.join(PHASES)}"
        try:
            target = (root / rel).resolve()
        except ValueError as e:
            return {}, f"phase {phase!r} is not a valid path: {rel} ({e})"
        if not target.is_relative_to(root):
            return {}, f"phase {phase!r} resolves outside the hook directory: {rel}"
        if not target.is_file():
            return {}, f"phase {phase!r} executable is not a file: {rel}"
        phases[phase] = str(target)
    return phases, ""


def hook_from_declaration(
    *,
    hook_id: str,
    display_name: str,
    version: str,
    declared: dict,
    directory: Path,
    source: str,
) -> HookRecord:
    """Build a HookRecord for hooks declared inside some other manifest.

    A plugin is the third source in the hooks directory, and it declares its
    phases in the same shape a hook.yaml does. Resolution lands here rather
    than in the plugin package so that phase names, directory confinement and
    the defaults keep one authority: a second implementation is a second place
    for the confinement check to drift out of.

    `declared` is assumed schema-validated (a non-empty mapping of string to
    string) by whichever manifest carried it. Everything past that shape is
    checked here.

    Defaults, deliberately not overridable from the carrying manifest: the
    default timeout, and `scope: agent`. A hook that reached the machine as a
    side effect of installing a plugin cannot declare itself workspace-wide;
    fleet-wide enforcement stays something an operator installs on purpose.
    """
    phases, reason = _resolve_phases(declared, directory)
    if reason:
        return replace(_disabled(hook_id, reason), source=source)
    return HookRecord(
        id=hook_id,
        display_name=display_name,
        version=version,
        phases=phases,
        timeout_ms=_DEFAULT_TIMEOUT_MS,
        scope=_DEFAULT_SCOPE,
        source=source,
        enabled=True,
        reason="",
    )


def load_manifest(path: Path) -> HookRecord:
    fallback_id = path.parent.name or "unknown"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _disabled(fallback_id, f"cannot read manifest: {e}")

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return _disabled(fallback_id, f"malformed YAML: {e}")

    if not isinstance(data, dict):
        return _disabled(fallback_id, "manifest is not a mapping")

    hook_id = data.get("id") or fallback_id

    try:
        jsonschema.validate(data, _SCHEMA)
    except jsonschema.ValidationError as e:
        return _disabled(hook_id, f"schema: {e.message}")

    timeout_ms = data.get("timeout_ms", _DEFAULT_TIMEOUT_MS)
    if timeout_ms <= 0:
        return _disabled(hook_id, f"timeout_ms must be greater than 0, got {timeout_ms}")

    scope = data.get("scope", _DEFAULT_SCOPE)
    if scope not in SCOPES:
        return _disabled(hook_id, f"unknown scope {scope!r}; valid scopes: {', '.join(SCOPES)}")

    phases, reason = _resolve_phases(data["hooks"], path.parent)
    if reason:
        return _disabled(hook_id, reason)

    return HookRecord(
        id=data["id"],
        display_name=data["displayName"],
        version=data["version"],
        phases=phases,
        timeout_ms=timeout_ms,
        scope=scope,
        source="",
        data=data,
        enabled=True,
        reason="",
    )
