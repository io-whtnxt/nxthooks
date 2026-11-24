import os
import subprocess

import pytest

from nxthooks import cli, lifecycle


@pytest.mark.parametrize("dry_run", [True, False])
def test_refresh_only_dispatchers(hooks_machine, monkeypatch, capsys, dry_run):
    host = hooks_machine("claude-code", "codex")
    monkeypatch.setattr(lifecycle.Host, "local", lambda: host)
    root = host.root()
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("cc-dispatch.sh", "codex-dispatch.sh"):
        (bin_dir / name).write_text("old transport")
        (bin_dir / name).chmod(0o755)
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
    for script in bin_dir.iterdir():
        assert str(script) in output
        if dry_run:
            assert script.read_text() == "old transport"
        else:
            assert os.access(script, os.X_OK)
            assert subprocess.check_output(["sh", str(script)], text=True) == "interpreter invoked"


@pytest.mark.parametrize("interpreter", ["python", "/nonexistent/python"])
def test_refresh_refuses_invalid_interpreter(hooks_machine, monkeypatch, interpreter):
    host = hooks_machine()
    monkeypatch.setattr(lifecycle.Host, "local", lambda: host)
    before = set(host.home.rglob("*"))
    assert cli.main(["refresh-dispatchers", "--python", interpreter]) == 1
    assert set(host.home.rglob("*")) == before
