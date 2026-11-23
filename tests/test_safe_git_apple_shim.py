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
    shim_dir = tmp_path / "shimdir"
    shim_dir.mkdir()
    fake_shim = _write(shim_dir / "git",
                       f"#!/bin/sh\necho executed >> {shim_dir / 'sentinel'}\nexit 69\n")
    stand_dir = tmp_path / "standaloned"
    stand_dir.mkdir()
    _write(stand_dir / "git", "#!/bin/sh\nexit 0\n")
    darwin.setattr(shim, "_SYSTEM_GIT_SHIM", os.path.realpath(str(fake_shim)))
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
    assert shim._find_git({"PATH": os.environ["PATH"]}) == os.path.realpath(str(rig["stand"]))


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


def test_toolchain_probe_honors_budget_without_spawning(tmp_path, darwin, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a cancelled probe must not spawn")
    monkeypatch.setattr(shim.subprocess, "Popen", refuse)
    with shim.commit_preparation_budget(float("-inf"), lambda: True):
        assert shim._toolchain_present() is False


def test_toolchain_probe_fails_closed_on_overflow(tmp_path, darwin, monkeypatch):
    noisy = _write(tmp_path / "noisy-select.sh", "#!/bin/sh\nhead -c 8192 /dev/zero\n")
    monkeypatch.setattr(shim, "_XCODE_SELECT", str(noisy))
    assert shim._toolchain_present() is False


def test_multiple_aliases_probe_the_toolchain_once(tmp_path, rig, darwin, monkeypatch):
    calls = []
    monkeypatch.setattr(shim, "_toolchain_present", lambda: (calls.append(1), False)[1])
    first, second = tmp_path / "first", tmp_path / "second"
    for slot in (first, second):
        slot.mkdir()
        (slot / "git").symlink_to(rig["shim"])
    path = f"{first}{os.pathsep}{second}"
    assert shim._find_git({"PATH": path}) is None
    assert calls == [1]


def test_path_entry_with_trailing_space_is_literal(tmp_path, rig, darwin):
    spaced = tmp_path / "with space "
    spaced.mkdir()
    wanted = _write(spaced / "git", "#!/bin/sh\nexit 0\n")
    neighbor = tmp_path / "with space"
    neighbor.mkdir()
    _write(neighbor / "git", "#!/bin/sh\nexit 3\n")
    path = f"{spaced}{os.pathsep}{neighbor}"
    assert shim._find_git({"PATH": path}) == os.path.realpath(str(wanted))


def test_toolchain_answer_must_name_a_usable_developer_dir(tmp_path, darwin, monkeypatch):
    def printer(text):
        return _write(tmp_path / "select.sh", f"#!/bin/sh\necho '{text}'\n")
    monkeypatch.setattr(shim, "_XCODE_SELECT", str(printer("")))
    assert shim._toolchain_present() is False
    monkeypatch.setattr(shim, "_XCODE_SELECT", str(printer("Library/Developer/CommandLineTools")))
    assert shim._toolchain_present() is False
    bare = tmp_path / "bare-toolchain"
    (bare / "usr" / "bin").mkdir(parents=True)
    monkeypatch.setattr(shim, "_XCODE_SELECT", str(printer(str(bare))))
    assert shim._toolchain_present() is False
    _write(bare / "usr" / "bin" / "git", "#!/bin/sh\nexit 0\n")
    assert shim._toolchain_present() is True


def test_empty_path_with_cwd_git_is_missing_and_never_runs(tmp_path, darwin, monkeypatch):
    _write(tmp_path / "git", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)
    _never_spawn(monkeypatch)
    assert run_git(["status"], str(tmp_path), timeout=5).outcome == "missing"


def test_alias_to_the_shim_counts_as_the_shim(tmp_path, rig, darwin, monkeypatch):
    alias_dir = tmp_path / "aliasdir"
    alias_dir.mkdir()
    (alias_dir / "git").symlink_to(rig["shim"])
    darwin.setenv("PATH", str(alias_dir))
    monkeypatch.setattr(shim, "_toolchain_present", lambda: False)
    assert shim._find_git({"PATH": str(alias_dir)}) is None
    monkeypatch.setattr(shim, "_toolchain_present", lambda: True)
    assert shim._find_git({"PATH": str(alias_dir)}) == os.path.realpath(str(rig["shim"]))


def test_symlinked_ancestor_still_classifies_the_shim(tmp_path, rig, darwin, monkeypatch):
    linkroot = tmp_path / "linkroot"
    linkroot.symlink_to(tmp_path, target_is_directory=True)
    darwin.setenv("PATH", str(linkroot / "shimdir"))
    monkeypatch.setattr(shim, "_toolchain_present", lambda: False)
    _never_spawn(monkeypatch)
    assert run_git(["status"], str(tmp_path), timeout=5).outcome == "missing"
    assert not rig["sentinel"].exists()
