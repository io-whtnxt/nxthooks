"""The Apple git shim must never be executed without a toolchain (T2412).

On macOS `/usr/bin/git` is a stub that summons the CLT install dialog when
run with no toolchain installed. `run_git` must resolve past it -- to a
standalone git later in PATH, or to `missing` -- and only count it when
`xcode-select -p` answers. Nothing here executes the shim.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from nxthooks import _safe_git as shim
from nxthooks._safe_git import _Outcome, run_git


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    return monkeypatch


@pytest.fixture
def rig(tmp_path, darwin):
    """A fake shim that records execution, a fake standalone, and the module
    pointed at the fake shim as the system one."""
    sentinel = tmp_path / "shim-ran"
    shim_dir = tmp_path / "shimdir"
    shim_dir.mkdir()
    fake_shim = _write(shim_dir / "git",
                       f"#!/bin/sh\necho executed >> {shim_dir / 'sentinel'}\nexit 69\n")
    stand_dir = tmp_path / "standaloned"
    stand_dir.mkdir()
    _write(stand_dir / "git", "#!/bin/sh\nexit 0\n")
    darwin.setattr(shim, "_SYSTEM_GIT_SHIM", str(fake_shim))
    darwin.setenv("PATH", f"{shim_dir}{os.pathsep}{stand_dir}")
    return {"shim": fake_shim, "sentinel": shim_dir / "sentinel",
            "stand": stand_dir / "git"}


def _never_spawn(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"git must not run here: {args[0][:2]}")
    monkeypatch.setattr(shim, "_spawn", refuse)


def test_shim_only_path_without_toolchain_is_missing_and_never_runs(tmp_path, rig, darwin, monkeypatch):
    darwin.setenv("PATH", str(rig["shim"].parent))
    monkeypatch.setattr(shim, "_toolchain_present", lambda: False)
    _never_spawn(monkeypatch)
    assert run_git(["status"], str(tmp_path), timeout=5).outcome == "missing"
    assert not rig["sentinel"].exists()


def test_shim_yields_to_standalone_later_in_path(tmp_path, rig, darwin, monkeypatch):
    monkeypatch.setattr(shim, "_toolchain_present", lambda: False)
    done = run_git(["status"], str(tmp_path), timeout=10)
    assert done.outcome == "ok", (done.outcome, done.refusal, done.stderr)
    assert not rig["sentinel"].exists()


def test_standalone_before_shim_never_probes_the_toolchain(tmp_path, rig, darwin, monkeypatch):
    darwin.setenv("PATH", f"{rig['stand'].parent}{os.pathsep}{rig['shim'].parent}")

    def refuse_probe():
        raise AssertionError("no probe is needed past a usable git")
    monkeypatch.setattr(shim, "_toolchain_present", refuse_probe)
    assert run_git(["status"], str(tmp_path), timeout=10).outcome == "ok"
    assert not rig["sentinel"].exists()


def test_working_toolchain_selects_the_shim_path_without_running_it(tmp_path, rig, darwin, monkeypatch):
    darwin.setenv("PATH", str(rig["shim"].parent))
    monkeypatch.setattr(shim, "_toolchain_present", lambda: True)
    seen = []
    monkeypatch.setattr(shim, "_spawn",
                        lambda argv, *a, **k: (seen.append(argv[0]),
                                              _Outcome(0, b"", b"", False, False))[1])
    assert run_git(["status"], str(tmp_path), timeout=10).outcome == "ok"
    assert seen and all(arg == str(rig["shim"]) for arg in seen)
    assert not rig["sentinel"].exists()


def test_non_darwin_keeps_which_resolution(tmp_path, rig, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PATH", str(rig["shim"].parent))
    assert shim._find_git({"PATH": str(rig["shim"].parent)}) == str(rig["shim"])


def test_toolchain_probe_fails_closed_on_errors(tmp_path, darwin, monkeypatch):
    monkeypatch.setattr(shim, "_XCODE_SELECT", "/nonexistent/xcode-select")
    assert shim._toolchain_present() is False


def test_toolchain_probe_fails_closed_on_timeout(tmp_path, darwin, monkeypatch):
    slow = _write(tmp_path / "slow-select.sh", "#!/bin/sh\nsleep 30\n")
    monkeypatch.setattr(shim, "_XCODE_SELECT", str(slow))
    monkeypatch.setattr(shim, "_TOOLCHAIN_TIMEOUT", 0.05)
    assert shim._toolchain_present() is False


def test_alias_to_the_shim_counts_as_the_shim(tmp_path, rig, darwin):
    alias_dir = tmp_path / "aliasdir"
    alias_dir.mkdir()
    alias = alias_dir / "git"
    alias.symlink_to(rig["shim"])
    assert shim._is_system_shim(str(alias)) is True
    assert shim._is_system_shim(str(rig["stand"])) is False
