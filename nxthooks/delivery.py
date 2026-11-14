"""NxtHooks puts its own pane on the machine, and takes it away.

`plugin.yaml` in this package describes the NxtHooks pane in NxtOS. `install`
copies it into the operator's NxtOS plugin root and `uninstall` removes it, so
the pane exists exactly while the component does. That is all this module does,
and it is deliberately not NxtOS's plugin delivery: that one validates a
manifest against the host's schema, resolves legacy roots, and serves every
component. NxtOS's delivery calls this one for the copy; this one never calls
NxtOS. Nor is the manifest always the only file it leaves behind: the host also
writes the agent documents the manifest declares, which is why `withdraw`
reads that manifest instead of recognising one filename.

It does not validate the manifest. The host refuses a malformed one at read
time, and the NxtOS suite loads this package's `plugin.yaml` through the real
loader, so a bad manifest fails in CI rather than on an operator's machine.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import _paths


def plugins_dir(home: Path, env: dict | None = None) -> Path:
    """The operator's NxtOS plugin root, `<data>/nxt/os/plugins`."""
    return _paths.resolve("os", "data", "plugins", env=env, home=home)


def deliver(source: Path, plugin_id: str, home: Path, env: dict | None = None) -> tuple[Path | None, str]:
    """Copy `source` to `<plugins>/<plugin_id>/plugin.yaml`.

    Returns the path written, or None and a reason. Never raises: it is called
    from inside `install`, which reports rather than aborts.
    """
    target = plugins_dir(home, env) / plugin_id / "plugin.yaml"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(source).read_bytes())
    except OSError as exc:
        return None, f"could not write {target}: {exc}"
    return target, ""


def withdraw(plugin_id: str, home: Path, env: dict | None = None) -> tuple[Path | None, str]:
    """Remove the manifest and the agent documents it declares.

    A plugin directory can hold more than its manifest, and the rule has
    always been that this function takes out what the component wrote and
    nothing else. What it wrote is not just `plugin.yaml`: the host's delivery
    copies every `agents:` entry in the manifest beside it, so a withdrawal
    that removed only the manifest handed the machine a document NxtOS still
    resolves as a plugin agent, from a component that is gone.

    So the declaration is read from the *installed* manifest before it is
    unlinked -- not from a source tree, which may since have changed -- and
    each entry is confined the way the host confined it on the way in (see
    `_declared_agents`). Directories the removal emptied go with their file,
    down to but never including the plugin directory itself, which goes only
    when nothing is left in it. A file the manifest never named is never
    touched, whatever it is.

    Returns the path removed, or None. The reason is non-empty only when an
    unlink actually failed: the Plugin SDK raises on a non-empty reason, so a
    declaration this package could not read must stay quiet and cost a stale
    document, not fail someone else's removal.
    """
    plugin_dir = plugins_dir(home, env) / plugin_id
    target = plugin_dir / "plugin.yaml"
    if not target.exists():
        return None, ""
    for path in _declared_agents(target):
        try:
            path.unlink()
        except FileNotFoundError:
            continue  # taken out between the check and the unlink: the goal state already holds
        except OSError as exc:
            return None, f"could not remove {path}: {exc}"
        _prune_empty(path.parent, plugin_dir)
    try:
        target.unlink()
        if not any(target.parent.iterdir()):
            target.parent.rmdir()
    except OSError as exc:
        return None, f"could not remove {target}: {exc}"
    return target, ""


def _prune_empty(start: Path, plugin_dir: Path) -> None:
    """The directories an unlink emptied, up to but not including the plugin
    directory itself -- which `withdraw` removes only once it is empty, and
    whose own parent belongs to the host. Both ends are compared resolved, so
    an entry written `agents/../x.md` stops at the plugin directory instead of
    walking above it, and a directory that still holds anything, declared or
    not, is left standing."""
    root = plugin_dir.resolve()
    parent = start.resolve()
    while parent != root and parent.is_relative_to(root):
        try:
            if any(parent.iterdir()):
                return
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _declared_agents(manifest: Path) -> list[Path]:
    """The agent documents `manifest` declares, as paths safe to unlink.

    Confinement mirrors the host's `_resolve_agent_paths`, which decided what
    to write: resolved *before* the containment test, so a `..` entry or a
    symlink pointing out of the plugin directory is refused rather than
    followed, and refused before the existence test, because `/etc/passwd`
    does exist. Absolute entries are refused by the same test -- `Path.__truediv__`
    discards the left side for an absolute right side, which is the escape.
    Only regular files survive: a declared directory is never a licence to
    recurse into it.

    Returns nothing when the declaration cannot be read, which is the same
    answer as "declares nothing" as far as the caller is concerned and the
    conservative one: an unremovable document is a stale pane, a wrongly
    unlinked neighbour is data loss. A symlinked plugin directory is refused
    outright -- `is_relative_to` compares resolved paths, so a directory
    symlinked elsewhere would pass its own test at the expense of whatever
    real directory it points at.

    Paths are returned unresolved so that a confined symlink costs its own
    link and never the file it points at.
    """
    directory = manifest.parent
    if directory.is_symlink():
        return []
    entries = _agents_entries(manifest)
    if entries is None:
        return []
    root = directory.resolve()
    paths: list[Path] = []
    for rel in entries:
        if not isinstance(rel, str) or not rel:
            continue
        path = directory / rel
        try:
            resolved = path.resolve()
        except (OSError, ValueError):
            continue  # ValueError: an embedded NUL byte is not a resolvable path
        if not resolved.is_relative_to(root) or not resolved.is_file():
            continue
        if resolved == manifest.resolve():
            continue  # a manifest is not an agent document, whatever it declares
        paths.append(path)
    return paths


def _agents_entries(manifest: Path) -> list | None:
    """The raw `agents:` list of a manifest, or None when it cannot be read.

    PyYAML is used when it is importable and this package degrades to
    `_agents_entries_without_yaml` when it is not, the same way the Hermes
    reader in `projection.verify` does. The degradation matters because the
    documents were written by the host, which requires PyYAML, while
    `nxthooks uninstall` runs from this package, which does not have it.
    """
    try:
        text = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        import yaml
    except ImportError:
        return _agents_entries_without_yaml(text)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    declared = data.get("agents")
    if declared is None:
        return []
    if not isinstance(declared, list) or not all(isinstance(entry, str) for entry in declared):
        return None  # a malformed declaration is not a readable one
    return declared


#: A top-level `agents:` key, and nothing else may share the line.
_AGENTS_KEY = re.compile(r"^agents:[ \t]*(.*)$")
#: One item of a block sequence. The indentation YAML allows at column 0 is
#: allowed here too, which is why the leading whitespace is not required.
_AGENTS_ITEM = re.compile(r"[ \t]*-[ \t]*(.*[^ \t])[ \t]*$")


def _agents_entries_without_yaml(text: str) -> list | None:
    """`agents:` entries with the standard library alone.

    Narrow on purpose: a top-level `agents:` followed by a block sequence of
    plain or single-quoted items, and None for anything else -- flow style, an
    anchor, a double-quoted string with escapes, an item YAML would read as a
    mapping rather than a string, a line continuing one item onto the next, or
    the key appearing twice (PyYAML keeps the last, this reader would have
    kept the first). Guessing at those would mean deleting a file the manifest
    never declared, which is the one thing this module may not do; stopping
    costs a stale document instead. Reaching here is rare: PyYAML is importable
    on any machine whose host delivered agents in the first place.
    """
    items: list[str] = []
    state = "seek"  # seek -> block -> done, so a second `agents:` is visible
    indent = 0
    for line in text.splitlines():
        if state == "seek":
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = _AGENTS_KEY.match(line)
            if match is None:
                continue
            if match.group(1):
                return None  # inline value: flow style or a scalar, both unread here
            state = "block"
            continue
        if state == "done":
            return None if _AGENTS_KEY.match(line) else items
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _AGENTS_KEY.match(line):
            return None      # the key appears twice; PyYAML would keep the later block
        match = _AGENTS_ITEM.match(line)
        item_indent = len(line) - len(line.lstrip())
        if match is None:
            if item_indent > indent:
                return None  # a line continuing an item onto the next
            state = "done"   # the block ended on the next key
            continue
        if items and item_indent != indent:
            return None      # inconsistent indentation is not the block this reads
        if not items:
            indent = item_indent
        item = match.group(1)
        if item.startswith("'"):
            if not item.endswith("'"):
                return None
            item = item[1:-1].replace("''", "'")
        elif item[0] in "-\"`@!|>&*?%{[," or " #" in item or ": " in item or item.endswith(":"):
            return None  # not a plain scalar
        items.append(item)
    return items if state != "seek" else []
