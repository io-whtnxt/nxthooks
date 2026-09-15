"""Parse and validate one plugin manifest. Never raises.

A broken manifest yields a disabled record carrying a reason, so the UI can
show the plugin greyed out with an explanation. Silent omission is the
failure mode this exists to prevent.

A manifest may also declare `hooks:`, which is the third source in the hooks
directory beside the builtin and user trees. It is resolved here rather than in
the hook registry because only this module knows the plugin's own directory,
and that directory is what confines the phase executables.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml

from nxthooks._vendor.hooks.manifest import HookRecord, hook_from_declaration

from .placeholders import placeholder_names

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema" / "nxtos-plugin.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

# external stays rejected: nothing in Wave A gives it an adapter. Adding a kind
# here means giving it one, the same bar cli, service, and agents all cleared.
_SUPPORTED_KINDS = ("cli", "service", "agents")


@dataclass(frozen=True)
class PluginRecord:
    """One installed plugin, as discovery found it.

    `enabled` is the effective answer to "is this plugin live", which is the
    question every consumer on the tool-call path asks. Two independent things
    can make it False, and they are not interchangeable: a manifest this module
    refused, which carries a `reason`, and a plugin the operator turned off,
    which sets `disabled_by_operator`. Folding operator intent into this one
    field is deliberate — `plugins.tools._declared_tools` and
    `hooks.registry.plugin_hooks` both read exactly it, so a second flag beside
    it would be an off switch that greys out a label and leaves the plugin
    answering tool calls.

    A refused manifest is stated first: a plugin that will not load is not off,
    it is broken, and telling an operator they turned it off would send them
    looking in the wrong place.
    """

    id: str
    display_name: str
    kind: str
    version: str
    data: dict = field(default_factory=dict)
    enabled: bool = False
    reason: str = ""
    # One plugin declares at most one hook, keyed by the plugin's own id, the
    # way its `hooks:` block is one phase map. None means the manifest never
    # mentioned hooks; a disabled record means it did and the declaration is
    # broken. The two are different states and the listing shows both.
    hook: HookRecord | None = None
    disabled_by_operator: bool = False
    # `agents:` entries, resolved to absolute paths and confined to `path`
    # (see `_resolve_agent_paths`). An entry that escapes the plugin
    # directory or does not resolve to a file is dropped here rather than
    # carried forward for `nodeagent.agents` to reject: this is the only
    # module that knows the plugin's own directory, and confinement is a
    # security property, not a content-validation one, so it belongs beside
    # `_declared_hook`'s phase-path confinement rather than duplicated in a
    # consumer that only knows the NxtAgent doc schema.
    agent_paths: tuple[Path, ...] = ()

    @property
    def manifest_ok(self) -> bool:
        """True when the manifest parsed and is supported, whatever the
        operator then decided about it. What the listing consults to decide
        whether it can still say what this plugin would do."""
        return self.enabled or self.disabled_by_operator


def _disabled(plugin_id: str, reason: str) -> PluginRecord:
    id_str = str(plugin_id)
    return PluginRecord(
        id=id_str, display_name=id_str, kind="", version="",
        enabled=False, reason=reason,
    )


def load_manifest(path: Path) -> PluginRecord:
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

    plugin_id = data.get("id") or fallback_id

    try:
        jsonschema.validate(data, _SCHEMA)
    except jsonschema.ValidationError as e:
        return _disabled(plugin_id, f"schema: {e.message}")

    if data["kind"] not in _SUPPORTED_KINDS:
        return _disabled(plugin_id, f"kind {data['kind']!r} not supported in v1")

    if "cli" in data:
        problem = _argument_pairing(data["cli"]["commands"])
        if problem:
            return _disabled(plugin_id, problem)

    if data["kind"] == "service":
        command_names = {c["name"] for c in data["cli"]["commands"]}
        undeclared = [t for t in data["provides_tools"] if t not in command_names]
        if undeclared:
            return _disabled(
                plugin_id,
                f"provides_tools references undefined command(s): {', '.join(undeclared)}",
            )

    return PluginRecord(
        id=data["id"],
        display_name=data["displayName"],
        kind=data["kind"],
        version=data["version"],
        data=data,
        enabled=True,
        reason="",
        hook=_declared_hook(data, path.parent),
        agent_paths=_resolve_agent_paths(data.get("agents", []), path.parent),
    )


def _argument_pairing(commands: list[dict]) -> str:
    """Refuse a command whose argv and `args` do not describe the same thing.

    Three ways they can disagree, all of which used to produce a form that runs
    the wrong command rather than an error anyone sees:

    A placeholder with no argument behind it is a value the form has no field
    for, so the run dies in `render_argv` with a KeyError the operator reads as
    "the plugin is broken" long after the manifest could have said so.

    An argument with no placeholder is a field the operator fills and the
    command never receives.

    And optionality has to agree, which is the pairing N6 actually names.
    `required: false` is only meaningful against a `{name?}`, because that is
    the only placeholder that can be left out; against a `{name}` it is a
    promise the template cannot keep, and the empty value goes to the command
    as `--flag ""`. The other direction is the quieter fault: a `{name?}` for an
    argument marked required renders a form that insists on a value the
    template was built to omit.
    """
    for cmd in commands:
        placeholders = placeholder_names(cmd["argv"])
        declared = {a["name"]: bool(a.get("required", False)) for a in cmd.get("args", [])}

        for name in sorted(set(placeholders) - set(declared)):
            return (f"command {cmd['name']!r} substitutes {{{name}}} but declares "
                    f"no argument {name!r}")
        for name in sorted(set(declared) - set(placeholders)):
            return (f"command {cmd['name']!r} declares argument {name!r} and never "
                    f"substitutes it")
        for name, optional in sorted(placeholders.items()):
            if optional and declared[name]:
                return (f"command {cmd['name']!r} argument {name!r} is required but "
                        f"substituted as {{{name}?}}, which may be omitted")
            if not optional and not declared[name]:
                return (f"command {cmd['name']!r} argument {name!r} is optional but "
                        f"substituted as {{{name}}}, which cannot be omitted; write "
                        f"{{{name}?}} or mark it required")
    return ""


def _resolve_agent_paths(declared: list, plugin_dir: Path) -> tuple[Path, ...]:
    """Resolve and confine `agents:` entries to the plugin's own directory.

    Mirrors `_resolve_phases`'s confinement order: checked after symlink
    resolution and before existence, because `../../../etc/passwd` names a
    file that does exist, so an existence check alone would let a manifest
    hand a path resolver a target outside its own directory. `root / rel`
    also silently discards `root` when `rel` is itself an absolute string
    (`Path.__truediv__`'s documented behaviour), which is exactly the case
    `is_relative_to` catches here the same way it catches a `..` escape.
    `.resolve()` itself raises `ValueError` for a string carrying an
    embedded NUL byte (valid YAML, e.g. `"agents/ bad.md"` with a `\x00`),
    which without a guard would propagate out of `load_manifest` and past
    `plugins.registry.scan` unbroken; caught here for the same reason
    everything else in this loop is caught here rather than by a caller.

    Traversal is nonetheless low severity for this particular declaration:
    an agent doc's frontmatter is a permission policy
    (`auto_approve_writes_in_cwd` is a standing auto-approval grant), and the
    plugin's own manifest author already has full authorship of that policy
    inside their own directory, so escaping it buys them less control over
    what they can already declare, not more. Confinement is enforced anyway,
    on the same principle `_resolve_phases` already applies to hooks: this
    module should never hand a consumer a path outside the directory it
    itself vouches for.

    An entry that fails confinement, or does not resolve to a file, is
    dropped rather than disabling the whole `agents:` declaration or the
    plugin: `agents:` is a list of independent claims, so one bad entry
    should cost exactly the one agent it names. Reading and validating each
    surviving path's content is `nodeagent.agents`'s job, since only that
    module knows the NxtAgent schema; this function only decides which paths
    are safe to hand it.
    """
    root = plugin_dir.resolve()
    resolved: list[Path] = []
    for rel in declared:
        try:
            target = (root / rel).resolve()
        except ValueError:
            continue  # e.g. an embedded NUL byte; not a resolvable path
        if not target.is_relative_to(root):
            continue
        if not target.is_file():
            continue
        resolved.append(target)
    return tuple(resolved)


def _declared_hook(data: dict, plugin_dir: Path) -> HookRecord | None:
    """Resolve `hooks:` against the plugin's own directory.

    Reached only once the plugin itself has been accepted, so a disabled
    plugin's hooks are never resolved: they are absent from discovery rather
    than present and unreachable, which is how `_declared_tools` already treats
    that plugin's provides_tools.

    A declaration that will not resolve disables the hook and not the plugin.
    The hook and the plugin's tools fail independently, and taking the tools
    down for a bad phase path would be a wider blast radius than the fault.
    """
    declared = data.get("hooks")
    if not declared:
        return None
    return hook_from_declaration(
        hook_id=data["id"],
        display_name=data["displayName"],
        version=data["version"],
        declared=declared,
        directory=plugin_dir,
        source=f"plugin:{data['id']}",
    )
