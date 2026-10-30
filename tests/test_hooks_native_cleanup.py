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


def test_the_installed_runtime_carries_everything_dispatch_imports(hooks_machine):
    """No `_framework` copy: the package ships its own paths and projection, so
    the one directory `run_nxthooks.py` puts on sys.path is the whole runtime."""
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    lib = host.root() / "lib"
    assert (lib / "nxthooks" / "_paths.py").is_file()
    assert (lib / "nxthooks" / "projection" / "__init__.py").is_file()
    assert not (lib / lifecycle.LEGACY_FRAMEWORK_DIR).exists()
    assert not (lib / "nxtoskit").exists()

    # The real launcher, not a copy of what it is believed to do: `run_path`
    # executes its module body while leaving `__name__` something other than
    # `__main__`, so `main()` never runs.
    probe = subprocess.run(
        [sys.executable, "-c",
         f"import runpy, sys, json; before = list(sys.path); runpy.run_path({str(lib / 'run_nxthooks.py')!r}); "
         "print(json.dumps([p for p in sys.path if p not in before]))"],
        cwd=host.home, capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout.strip().splitlines()[-1]) == [str(lib)]


def test_an_old_framework_copy_is_removed_by_the_next_install(hooks_machine):
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    stale = host.root() / "lib" / lifecycle.LEGACY_FRAMEWORK_DIR / "nxtoskit"
    stale.mkdir(parents=True)
    (stale / "paths.py").write_text("# an earlier install put this here\n")
    assert lifecycle.cmd_install(_args(), host) == 0
    assert not stale.parent.exists()


def test_reinstalling_never_leaves_the_runtime_missing(hooks_machine, monkeypatch):
    """Staged beside the live copy and renamed in. The old `rmtree` + `copytree`
    left `lib/nxthooks` absent for the length of the copy, and a fail-closed
    guard dispatched in that window blocked every tool call."""
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    live = host.root() / "lib" / "nxthooks"
    seen = []
    real = lifecycle.shutil.copytree

    def watched(src, dst, *a, **k):
        result = real(src, dst, *a, **k)
        if Path(dst).parent == live.parent and Path(dst) != live:
            seen.append((live / "cli.py").is_file())
        return result

    monkeypatch.setattr(lifecycle.shutil, "copytree", watched)
    assert lifecycle.cmd_install(_args(), host) == 0
    assert seen == [True]
    assert not live.with_name("nxthooks.staging").exists()
    assert not live.with_name("nxthooks.previous").exists()
    assert not live.with_name(".nxthooks.link").exists()


def test_the_lifecycle_lock_is_not_in_the_world_writable_temp_dir(hooks_machine):
    """`/tmp` is world-writable on a shared host and the name was fully
    derivable from a uid and a home directory, so any other local user could
    create the path first and hold `LOCK_EX` on it -- wedging every install,
    uninstall and doctor from then on with a message pointing at nothing.
    """
    host = hooks_machine("claude-code")
    path = operation_lock.lock_path(host)

    assert path.is_relative_to(host.home)

    # Asserted against the source, not against `gettempdir()`. The original
    # check was `not path.is_relative_to(gettempdir())`, which says nothing
    # here: `hooks_machine` builds its home under pytest's `tmp_path`, which
    # on Linux *is* inside /tmp -- so the test failed there on its own
    # sandbox. It passed on macOS only because `tmp_path` is
    # `/private/var/folders/...` while `gettempdir()` returns
    # `/var/folders/...`, and `is_relative_to` is textual. Green on one
    # platform by a missing path prefix, red on the other for no product
    # reason. What actually matters is that the lock location is derived from
    # the host and never from the system temp dir.
    source = Path(operation_lock.__file__).read_text(encoding="utf-8")
    assert "tempfile" not in source, (
        "the lifecycle lock must be derived from the host, not from the "
        "system temp dir: /tmp is world-writable on a shared host and the "
        "name was fully derivable, so any local user could create it first "
        "and hold LOCK_EX on it"
    )


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
