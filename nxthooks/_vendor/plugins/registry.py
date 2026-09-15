"""Filesystem plugin registry.

Scanning must never execute anything. Listing plugins happens on every UI
render; if listing could execute, every render would be an execution surface.
Enforced by tests/test_plugin_registry.py::test_scan_spawns_no_subprocess.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from nxthooks._vendor import paths

from .manifest import PluginRecord, load_manifest
from .preferences import disabled_plugin_ids

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"


def plugins_dir(home: Path) -> Path:
    """Plugins root. Adding a builtin plugin is dropping a manifest in
    BUILTIN_DIR, the way adding a builtin agent is dropping a definition in
    nodeagent/builtin_agents."""
    return paths.resolve("os", "data", "plugins", home=home)


def _collect(root: Path) -> dict[str, PluginRecord]:
    if not root.is_dir():
        return {}
    return {
        record.id: record
        for entry in sorted(root.iterdir())
        if entry.is_dir()
        for record in [load_manifest(entry / "plugin.yaml")]
    }


def builtin_plugins() -> dict[str, PluginRecord]:
    return _collect(BUILTIN_DIR)


def user_plugins(home: Path) -> dict[str, PluginRecord]:
    return _collect(plugins_dir(home))


def library_plugins(library: Path) -> dict[str, PluginRecord]:
    """Fleet-wide plugins. Tool CODE is shared; tool CREDENTIALS are not, and
    stay per deployment in agent-policy.yaml's mcp_servers grant."""
    return _collect(plugins_dir(library))


def _apply_operator_intent(record: PluginRecord, disabled: frozenset[str]) -> PluginRecord:
    """Fold what the operator decided into the record's effective `enabled`.

    Applied here and not in load_manifest because only the registry is given
    `home`, and because the two answers belong to different layers: the
    manifest says whether the plugin could run, this says whether it may.

    A refused manifest is left exactly as it is. It is already not live, and
    restating it as "you turned this off" would send an operator looking for a
    switch instead of at the reason.

    `data` and `hook` survive, so the listing can still say what turning the
    plugin back on would put on the tool-call path. Nothing reads them for a
    record whose `enabled` is False.
    """
    if not record.enabled or record.id not in disabled:
        return record
    return replace(record, enabled=False, disabled_by_operator=True)


def scan(home: Path, library: Path | None = None) -> list[PluginRecord]:
    """Resolve the plugin roster, least specific first.

    builtin < library < user, extending nodeagent.agents.load_agents' shape
    to plugins. The library is fleet-wide operator authorship one tier above
    builtin, not a mere specificity tier, so a user plugin replaces a builtin
    but must never replace one the library claims: filtering the library's
    ids out of the user update is what makes that refusal real rather than
    merely intended. `library=None` collects nothing from that tier, so a
    caller that never passes one sees exactly the old builtin/user roster.

    **Known gap, not a decision.** Hooks and agents both report a lost claim
    through a collision channel (`HookCollision`, `AgentCollision`); plugins
    have no such channel. A user plugin that loses its id to the library is
    therefore dropped here with nothing surfacing the loss to the operator.
    This has been noticed and left unfixed here on purpose, not overlooked:
    adding a `PluginCollision` channel is its own task with its own
    consumers, not a side effect of this one.

    Operator intent is applied last, over all three trees, because a library
    plugin is no less the operator's to turn off on this machine than a
    builtin one. Disabling only ever reduces: a deployment can turn a
    library plugin off, but `disabled_plugin_ids` can never grant it one its
    own agent-policy.yaml never authorized.
    """
    plugins = builtin_plugins()
    library_pkgs = library_plugins(library) if library is not None else {}
    plugins.update(library_pkgs)
    plugins.update({i: r for i, r in user_plugins(home).items() if i not in library_pkgs})
    disabled = disabled_plugin_ids(home)
    return sorted(
        (_apply_operator_intent(r, disabled) for r in plugins.values()),
        key=lambda r: r.display_name.lower(),
    )
