"""What the operator decided about the installed plugins and hooks.

`PluginRecord.enabled` (and `HookRecord.enabled`) answers whether a manifest
parsed and is supported. Whether the operator wants a sound one running is a
different question on a different axis, and it needs somewhere to live that a
plugin or hook cannot reach: something that could turn itself back on by
shipping a new manifest is not an off switch. This file is that somewhere, and
it sits beside the plugin tree rather than inside it so unpacking an archive
over a plugin directory cannot rewrite the operator's intent as a side effect.

Plugins and hooks share this one file but never one key. `disabled` holds
plugin ids, `disabled_hooks` holds hook ids; a plugin and a hook that happened
to share a name would otherwise disable each other.

Reading never raises. A missing, unreadable or malformed file means nothing
has been turned off, which is also the state of a fresh install; refusing to
list plugins or hooks because this file is corrupt would take the whole
surface down for a fault that has a safe default. The write path repairs it.
"""

from __future__ import annotations

import json
from pathlib import Path

from nxthooks._vendor import paths


class HookReferencedError(Exception):
    """Raised when disabling a hook an agent in the roster names.

    Refused rather than allowed, because `_require_installed_hooks` raises on
    a base-source agent naming an uninstalled hook, and a disabled hook is
    absent as far as `hooks_for_agent` is concerned. Allowing it would let a
    deployment blank its own roster from the Settings UI.
    """


def preferences_path(home: Path) -> Path:
    """The preferences file, beside the plugin tree rather than inside it."""
    return paths.resolve("os", "config", "plugin-preferences.json", home=home)


def _read(home: Path) -> dict:
    """The document as written, or an empty one for anything that is not a
    readable JSON object. One parser for both axes, so a corrupt file takes
    down neither surface rather than one and not the other."""
    try:
        data = json.loads(preferences_path(home).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _ids(document: dict, key: str) -> frozenset[str]:
    raw = document.get(key)
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(item for item in raw if isinstance(item, str))


def _write(home: Path, document: dict) -> None:
    path = preferences_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def disabled_plugin_ids(home: Path) -> frozenset[str]:
    """The plugin ids the operator has turned off."""
    return _ids(_read(home), "disabled")


def disabled_hook_ids(home: Path) -> frozenset[str]:
    """The hook ids the operator has turned off.

    Its own key, not `disabled`: that one holds plugin ids, and a hook and a
    plugin sharing a name would otherwise disable both.
    """
    return _ids(_read(home), "disabled_hooks")


def set_plugin_disabled(home: Path, plugin_id: str, disabled: bool) -> frozenset[str]:
    """Record one plugin's intent and return the full set afterwards.

    The whole set is rewritten rather than appended to, so the file cannot
    accumulate a duplicate, and it is sorted so two runs that reach the same
    intent produce the same bytes. Whatever the document already holds under
    `disabled_hooks` is carried forward untouched.
    """
    document = _read(home)
    ids = set(_ids(document, "disabled"))
    if disabled:
        ids.add(plugin_id)
    else:
        ids.discard(plugin_id)

    document["disabled"] = sorted(ids)
    document["disabled_hooks"] = sorted(_ids(document, "disabled_hooks"))
    _write(home, document)
    return frozenset(ids)


def set_hook_disabled(
    home: Path, hook_id: str, disabled: bool, referenced_by: dict[str, tuple[str, ...]] | None = None
) -> frozenset[str]:
    """Record one hook's intent and return the full set afterwards.

    `referenced_by` maps hook id to the agents naming it, as resolved from the
    live roster at the moment of the call. Re-enabling is never refused: it
    only ever restores, and a hook that is safe to turn back on can never be
    the thing that blanks a roster. Disabling is refused when the roster
    still names the hook, with the agent named in the message, because
    naming only the hook does not tell the operator what to change.

    Whatever the document already holds under `disabled` is carried forward
    untouched, the same way `set_plugin_disabled` preserves this key.

    No production caller yet. The hooks settings route that will call this is
    not built, and it belongs to whoever builds that surface; the capability
    is here because the spec mandates it and because the file is hand-editable
    in the meantime.
    """
    if disabled:
        agents = (referenced_by or {}).get(hook_id) or ()
        if agents:
            raise HookReferencedError(
                f"cannot disable hook {hook_id!r}: it is named by "
                f"{', '.join(sorted(agents))}. Disabling it would leave those "
                "agents naming an uninstalled hook, which refuses the whole "
                "roster. Change the agent, or disable a workspace-scope hook "
                "instead."
            )

    document = _read(home)
    ids = set(_ids(document, "disabled_hooks"))
    if disabled:
        ids.add(hook_id)
    else:
        ids.discard(hook_id)

    document["disabled_hooks"] = sorted(ids)
    document["disabled"] = sorted(_ids(document, "disabled"))
    _write(home, document)
    return frozenset(ids)
