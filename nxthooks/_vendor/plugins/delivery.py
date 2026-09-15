"""How a component puts its own pane on the machine, and takes it away.

A plugin manifest and a component are not two products. `nxthooks` ships a CLI
and a pane over that CLI, and both are the same files; the same holds for
skills and wiki. What was wrong until now was where the manifest lived: inside
`nxthooks._vendor/plugins/builtin/`, so the pane appeared because the *host* package
was importable, whether or not the component it describes had ever been
installed. Removing the component left its pane behind, and installing NxtOS
alone conjured panes for components the machine did not have.

So the manifest travels with the component and arrives through its `install`,
which is the only event that knows the component is really here. `uninstall`
takes it back out. That is the whole of N4-c-1, and it is why these two
functions exist rather than three copies of `shutil.copy`.

Two properties are worth stating because they are easy to lose:

**A component never installs a pane the host would refuse.** The manifest is
loaded through the same `load_manifest` the registry uses before a byte is
written, so a manifest that would show up in the roster as a greyed-out fault
never gets written at all. The install says why instead, at the moment someone
is watching.

**Withdrawal removes the file, not the directory's contents.** A plugin
directory can hold more than its manifest (agent documents, for one), and a
component that shipped only the manifest has no business deleting whatever else
came to live beside it. The directory goes only when taking the manifest out
left it empty.
"""

from __future__ import annotations

from pathlib import Path

from .manifest import load_manifest
from .registry import plugins_dir


def deliver(source: Path, home: Path) -> tuple[Path | None, str]:
    """Install one manifest into the operator's plugin root.

    Returns the path written, or None and a reason. Never raises: it is called
    from inside a component's `install`, which reports rather than aborts.
    """
    record = load_manifest(source)
    if not record.enabled:
        return None, f"{source}: {record.reason}"

    target = plugins_dir(home) / record.id / "plugin.yaml"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    except OSError as exc:
        return None, f"could not write {target}: {exc}"
    return target, ""


def withdraw(plugin_id: str, home: Path) -> tuple[Path | None, str]:
    """Take one manifest back out. Returns the path removed, or None."""
    target = plugins_dir(home) / plugin_id / "plugin.yaml"
    if not target.exists():
        return None, ""
    try:
        target.unlink()
        if not any(target.parent.iterdir()):
            target.parent.rmdir()
    except OSError as exc:
        return None, f"could not remove {target}: {exc}"
    return target, ""
