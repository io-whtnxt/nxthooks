"""NxtHooks puts its own pane on the machine, and takes it away.

`plugin.yaml` in this package describes the NxtHooks pane in NxtOS. `install`
copies it into the operator's NxtOS plugin root and `uninstall` removes it, so
the pane exists exactly while the component does. That is all this module does,
and it is deliberately not NxtOS's plugin delivery: that one validates a
manifest against the host's schema, resolves legacy roots, and serves every
component. NxtOS's delivery calls this one for the copy; this one never calls
NxtOS.

It does not validate the manifest. The host refuses a malformed one at read
time, and the NxtOS suite loads this package's `plugin.yaml` through the real
loader, so a bad manifest fails in CI rather than on an operator's machine.
"""

from __future__ import annotations

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
    """Remove the manifest, not the directory's other contents; the directory
    goes only when that left it empty. Returns the path removed, or None."""
    target = plugins_dir(home, env) / plugin_id / "plugin.yaml"
    if not target.exists():
        return None, ""
    try:
        target.unlink()
        if not any(target.parent.iterdir()):
            target.parent.rmdir()
    except OSError as exc:
        return None, f"could not remove {target}: {exc}"
    return target, ""
