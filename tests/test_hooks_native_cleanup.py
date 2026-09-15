"""Uninstall removes what this install put there, and nothing beside it.

`cmd_uninstall` used to `rmtree` the whole runtime root, the whole config
directory and the whole state directory. Those are directories an operator can
put their own files in -- a hand-written manifest under `hooks.d`, a note next
to the runtime -- and all three were taken with it. Install writes an inventory
now and uninstall removes exactly the files named in it.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from test_nxthooks_lifecycle_presence import _args
from nxthooks import lifecycle, operation_lock


def test_purge_preserves_canonical_hook_sources_and_unrelated_files(hooks_machine, capsys):
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    manifest = host.hooks_d / "operator.toml"
    manifest.write_text("# operator source\n")
    config_note = host.config_dir / "operator.txt"
    config_note.write_text("keep config note")
    runtime_note = host.root() / "operator.txt"
    runtime_note.write_text("keep runtime note")
    package_note = host.root() / "lib/nxthooks/operator.txt"
    package_note.write_text("keep package note")

    assert lifecycle.cmd_uninstall(_args(), host) == 0

    assert manifest.read_text() == "# operator source\n"
    assert config_note.read_text() == "keep config note"
    assert runtime_note.read_text() == "keep runtime note"
    assert package_note.read_text() == "keep package note"
    assert not host.config_path.exists()
    assert not (host.root() / "lib/nxthooks/cli.py").exists()


@pytest.mark.skip(reason="not applicable to the extracted package: the _framework copy is not written by a vendored build")
def test_installed_dispatch_has_its_own_framework_primitives(hooks_machine):
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    framework = host.root() / "lib" / lifecycle.FRAMEWORK_DIR / "nxthooks/_vendor"
    assert (framework / "paths.py").is_file()
    assert (framework / "projection" / "__init__.py").is_file()


@pytest.mark.skip(reason="not applicable to the extracted package: there is no partial nxtoskit to outrank anything")
def test_the_shipped_framework_is_not_placed_where_it_outranks_a_real_nxtoskit(hooks_machine):
    """What ships with the runtime is a *partial* `nxthooks._vendor`: paths and
    projection, nothing else. `run_nxthooks.py` exposes the whole CLI, and
    `cmd_install` and `cmd_uninstall` both import `nxthooks._vendor.plugins.delivery`,
    so at `lib/nxthooks._vendor` ahead of `sys.path` this copy turned both commands
    into `ModuleNotFoundError` on a machine with nxthooks._vendor fully installed.

    Asserted structurally, on where the launcher puts the directory, and
    deliberately not by importing `nxthooks._vendor.plugins` and seeing whether it
    resolves. That import succeeds here whatever the launcher does: this
    checkout installs nxthooks._vendor editable, and a setuptools editable install
    registers a `sys.meta_path` finder, which is consulted before `sys.path`
    is looked at at all. An import probe passes on a developer's machine and
    proves nothing about an operator's.
    """
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    lib = host.root() / "lib"
    assert not (lib / "nxthooks/_vendor").exists()

    # The real launcher, not a copy of what it is believed to do: `run_path`
    # executes its module body while leaving `__name__` something other than
    # `__main__`, so `main()` never runs.
    probe = subprocess.run(
        [sys.executable, "-c",
         f"import runpy, sys, json, sysconfig; runpy.run_path({str(lib / 'run_nxthooks.py')!r}); "
         "print(json.dumps({'path': sys.path, 'purelib': sysconfig.get_paths()['purelib']}))"],
        cwd=Path(lifecycle.__file__).resolve().parents[1],
        capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr
    resolved = json.loads(probe.stdout.strip().splitlines()[-1])

    framework = str(lib / lifecycle.FRAMEWORK_DIR)
    assert framework in resolved["path"]
    assert resolved["path"].index(framework) > resolved["path"].index(resolved["purelib"]), (
        "the shipped framework must sit behind the real site-packages, not ahead of it")
    # And the runtime's own package still has to win, which is why the two
    # directories cannot be the same one.
    assert resolved["path"].index(str(lib)) < resolved["path"].index(resolved["purelib"])


def test_the_lifecycle_lock_is_not_in_the_world_writable_temp_dir(hooks_machine):
    """`/tmp` is world-writable on a shared host and the name was fully
    derivable from a uid and a home directory, so any other local user could
    create the path first and hold `LOCK_EX` on it -- wedging every install,
    uninstall and doctor from then on with a message pointing at nothing.
    """
    host = hooks_machine("claude-code")
    path = operation_lock.lock_path(host)

    assert not path.is_relative_to(Path(tempfile.gettempdir()))
    assert path.is_relative_to(host.home)


def test_the_lock_is_not_inside_the_state_directory_uninstall_removes(hooks_machine):
    """It cannot live in `state_dir` either: `cmd_uninstall` removes that
    while the lock around it is still held, so the operation would be
    releasing a lock on a deleted inode and the next one would create a fresh
    file and inherit no exclusion from it.

    Leaving it behind is not the alternative. `uninstall` promises the
    machine is left as it was found, and the lock takes the directory it
    needed with it -- which `test_uninstall_leaves_the_machine_as_it_found_it`
    is the real check on.
    """
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    path = operation_lock.lock_path(host)
    assert not path.is_relative_to(host.state_dir)

    assert lifecycle.cmd_uninstall(_args(), host) == 0

    assert not path.exists()


def test_lifecycle_refuses_overlapping_native_operations(hooks_machine, capsys):
    from nxthooks.operation_lock import exclusive_lifecycle

    host = hooks_machine("claude-code")

    @exclusive_lifecycle
    def operation(args, selected):
        return lifecycle.cmd_install(args, selected)

    assert operation(_args(), host) == 1
    assert "Another native Hooks lifecycle" in capsys.readouterr().out
    assert not host.config_path.exists()


def test_a_corrupt_runtime_inventory_refuses_cleanly(hooks_machine):
    """The inventory is the file that has to survive a crash mid-install, so
    a corrupt one is the case it most has to answer for. Its shape was read
    carefully and then a decode error was let out as a traceback."""
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    (host.root() / lifecycle.RUNTIME_INVENTORY).write_text(
        '{"version": 1, "files": [', encoding="utf-8")

    with pytest.raises(ValueError, match="Unreadable native runtime inventory"):
        lifecycle.cmd_uninstall(_args(), host)


def test_an_unowned_runtime_is_preserved_and_not_announced_as_cleaned(tmp_path, capsys):
    """The two prints ran back to back, so an operator with no inventory and
    an unrecognised root was told the runtime was preserved and that cleanup
    had completed, in consecutive lines."""
    root = tmp_path / "runtime"
    (root / "lib").mkdir(parents=True)
    keep = root / "operator.txt"
    keep.write_text("mine")

    assert lifecycle._remove_runtime(root, known=False) is False

    assert "runtime preserved" in capsys.readouterr().out
    assert keep.read_text() == "mine"
