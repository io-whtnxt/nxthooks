"""Cross-platform paths and hook invocation.

NxtHooks ran only where bash and shebangs do. These cover the two foundations
that had to change before a hook could run on Windows at all: where state
lives, and how a hook source is launched.
"""

import sys
from pathlib import Path

import pytest

from nxthooks import platform as plat
from nxthooks.dispatch import _command_for
from nxthooks import _paths as paths


def _reload_with(monkeypatch, windows, env):
    # `plat` delegates to `nxtoskit.paths` since N1-a; the platform fork lives
    # there, so that is what a Windows test has to flip.
    monkeypatch.setattr(paths, "WINDOWS", windows)
    for key in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_posix_defaults_are_the_paths_already_in_use(monkeypatch):
    """Adopting XDG must not relocate an existing install."""
    _reload_with(monkeypatch, windows=False, env={})
    assert plat.config_dir() == Path.home() / ".config" / "nxt" / "hooks"
    assert plat.data_dir() == Path.home() / ".local" / "share" / "nxt" / "hooks"


def test_posix_honours_xdg_overrides(monkeypatch):
    _reload_with(monkeypatch, windows=False,
                 env={"XDG_CONFIG_HOME": "/cfg", "XDG_DATA_HOME": "/data"})
    assert plat.config_dir() == Path("/cfg/nxt/hooks")
    assert plat.data_dir() == Path("/data/nxt/hooks")


def test_windows_uses_appdata_not_dotconfig(monkeypatch):
    _reload_with(monkeypatch, windows=True,
                 env={"APPDATA": r"C:\Users\e\AppData\Roaming",
                      "LOCALAPPDATA": r"C:\Users\e\AppData\Local"})
    assert "Roaming" in str(plat.config_dir())
    assert "Local" in str(plat.data_dir())
    assert ".config" not in str(plat.config_dir())


def test_windows_falls_back_when_appdata_is_unset(monkeypatch):
    _reload_with(monkeypatch, windows=True, env={})
    assert plat.config_dir() == Path.home() / "AppData" / "Roaming" / "nxt" / "hooks"


def test_hooks_d_lives_under_config(monkeypatch):
    _reload_with(monkeypatch, windows=False, env={"XDG_CONFIG_HOME": "/cfg"})
    assert plat.hooks_d() == Path("/cfg/nxt/hooks/hooks.d")


# ------------------------------------------------------------------ argv ----

def test_python_hook_uses_the_dispatchers_own_interpreter(tmp_path):
    """The portable case, and the one Windows depends on entirely."""
    src = tmp_path / "hook.py"
    src.write_text("print('hi')")
    assert _command_for(src) == [sys.executable, str(src)]


def test_python_hook_ignores_a_conflicting_shebang(tmp_path):
    """Extension wins: a shebang naming another python must not repoint it."""
    src = tmp_path / "hook.py"
    src.write_text("#!/usr/bin/python2\nprint('hi')")
    src.chmod(0o755)
    assert _command_for(src) == [sys.executable, str(src)]


def test_shell_hook_is_named_as_bash(tmp_path):
    src = tmp_path / "hook.sh"
    src.write_text("#!/bin/bash\ntrue\n")
    assert _command_for(src) == ["bash", str(src)]


def test_extensionless_executable_still_honours_its_shebang(tmp_path):
    src = tmp_path / "hook"
    src.write_text("#!/usr/bin/env ruby\n")
    src.chmod(0o755)
    assert _command_for(src) == ["ruby", str(src)]


@pytest.mark.parametrize("suffix", [".py", ".sh"])
def test_argv_needs_no_execute_bit(tmp_path, suffix):
    """Windows has no execute bit; resolution must not depend on one."""
    src = tmp_path / f"hook{suffix}"
    src.write_text("x")
    src.chmod(0o644)
    assert _command_for(src)[1] == str(src)
