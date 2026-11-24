import fcntl
from pathlib import Path
import os
import stat
import sys
import subprocess

import pytest

from nxthooks import cli, lifecycle, operation_lock


@pytest.mark.parametrize("dry_run", [True, False])
def test_refresh_updates_only_interpreter_targets(hooks_machine, monkeypatch, capsys, dry_run):
    host = hooks_machine("claude-code", "codex")
    monkeypatch.setattr(lifecycle.Host, "local", lambda: host)
    root = host.root()
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("cc-dispatch.sh", "codex-dispatch.sh"):
        (bin_dir / name).write_text("old transport")
        (bin_dir / name).chmod(0o644)
    marker = root / "PYTHON"
    marker.write_text("old python\n")
    sentinel = root / "untouched"
    sentinel.write_text("preserve")
    interpreter = host.home / 'python $HOME "quoted"'
    interpreter.write_text("#!/bin/sh\nprintf '%s' 'interpreter invoked'\n")
    interpreter.chmod(0o755)
    runner = root / "lib" / "run_nxthooks.py"
    runner.parent.mkdir()
    runner.touch()
    before = set(host.home.rglob("*"))
    args = ["refresh-dispatchers", "--python", str(interpreter)]
    assert cli.main(args + (["--dry-run"] if dry_run else [])) == 0
    output = capsys.readouterr().out
    assert set(host.home.rglob("*")) == before
    assert sentinel.read_text() == "preserve"
    assert str(marker) in output
    assert marker.read_text() == ("old python\n" if dry_run else f"{interpreter}\n")
    for script in bin_dir.iterdir():
        assert str(script) in output
        if dry_run:
            assert script.read_text() == "old transport"
            assert stat.S_IMODE(script.stat().st_mode) == 0o644
        else:
            assert stat.S_IMODE(script.stat().st_mode) == 0o755
            assert subprocess.check_output(["sh", str(script)], text=True) == "interpreter invoked"


@pytest.mark.parametrize("interpreter", ["python", "/nonexistent/python"])
def test_refresh_refuses_invalid_interpreter(hooks_machine, monkeypatch, interpreter):
    host = hooks_machine()
    monkeypatch.setattr(lifecycle.Host, "local", lambda: host)
    before = set(host.home.rglob("*"))
    assert cli.main(["refresh-dispatchers", "--python", interpreter]) == 1
    assert set(host.home.rglob("*")) == before



@pytest.mark.parametrize("dry_run", [True, False])
def test_refresh_validates_every_target_before_printing(hooks_machine, monkeypatch, capsys, dry_run):
    host = hooks_machine()
    monkeypatch.setattr(lifecycle.Host, "local", lambda: host)
    root = host.root()
    (root / "bin").mkdir(parents=True)
    first = root / "bin" / "cc-dispatch.sh"
    first.write_text("old")
    args = ["refresh-dispatchers", "--python", sys.executable]
    assert cli.main(args + (["--dry-run"] if dry_run else [])) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "codex-dispatch.sh" in captured.err
    assert first.read_text() == "old"


def test_refresh_replaces_targets_under_lifecycle_lock(hooks_machine, monkeypatch):
    host = hooks_machine()
    monkeypatch.setattr(lifecycle.Host, "local", lambda: host)
    root = host.root()
    (root / "bin").mkdir(parents=True)
    targets = [root / "bin" / name for name in ("cc-dispatch.sh", "codex-dispatch.sh")]
    targets.append(root / "PYTHON")
    for target in targets:
        target.touch()
    original = os.replace
    replaced = []

    def replace(src, dst):
        with operation_lock.lock_path(host).open("r") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        replaced.append(dst)
        return original(src, dst)

    monkeypatch.setattr(lifecycle.os, "replace", replace)
    assert cli.main(["refresh-dispatchers", "--python", sys.executable]) == 0
    assert replaced == targets


def test_direct_dry_run_honours_the_given_host_and_writes_nothing(hooks_machine, capsys):
    """Review 00fef284: a direct caller with dry_run=True and its own host must get a preview of that host."""
    import argparse
    host = hooks_machine()
    root = host.root()
    (root / "bin").mkdir(parents=True)
    targets = [root / "bin" / name for name in ("cc-dispatch.sh", "codex-dispatch.sh")] + [root / "PYTHON"]
    for target in targets:
        target.write_text("old\n")
    args = argparse.Namespace(python=Path(sys.executable), dry_run=True)
    assert lifecycle.cmd_refresh_dispatchers(args, host) == 0
    assert all(target.read_text() == "old\n" for target in targets)
    assert [line.split(" ", 2)[2] for line in capsys.readouterr().out.splitlines()] == [str(t) for t in targets]
