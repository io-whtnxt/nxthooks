"""`install_runtime` switches the live runtime in one atomic step.

`lib/nxthooks` is what every harness dispatch imports, and the builtins are
fail-open: a dispatch that cannot import it runs unscanned. The two-rename swap
left it missing between the renames, and a crash there lost the rescued hook
sources it held only in memory. On POSIX `lib/nxthooks` is now a symlink to a
versioned sibling, flipped with one `os.replace`.
"""

import os
import sys
from pathlib import Path

import pytest

from nxthooks import lifecycle
from test_nxthooks_lifecycle_presence import _args


def _install(host):
    return lifecycle.install_runtime(host, {"install": {"root": str(host.home / "rt")}})


def _live(root: Path) -> Path:
    return root / "lib" / "nxthooks"


def _ours(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "lib").iterdir() if "nxthooks" in p.name
                  and p.name != "run_nxthooks.py")


def _seed_foreign(host, monkeypatch):
    """Install, then point a live manifest at a source inside the runtime."""
    root = _install(host)
    source = _live(root) / "adapters" / "nlm-session-start.py"
    source.write_text("print('irreplaceable')\n")
    monkeypatch.setattr(lifecycle, "load_manifests",
                        lambda *a, **k: [{"name": "nlm", "source": str(source)}])
    return root, source


def _layout(monkeypatch, atomic):
    if atomic and os.name == "nt":
        pytest.skip("the symlink flip is POSIX")
    monkeypatch.setattr(lifecycle, "_ATOMIC_LINK", atomic)


@pytest.mark.skipif(os.name == "nt", reason="the symlink flip is POSIX")
def test_the_runtime_is_a_symlink_to_a_versioned_sibling(hooks_machine):
    root = _install(hooks_machine())
    live = _live(root)
    assert live.is_symlink()
    target = live.resolve()
    assert target.parent == (root / "lib").resolve()
    assert target.name.startswith(".nxthooks-" + lifecycle.__version__)
    assert (live / "cli.py").is_file()


@pytest.mark.skipif(os.name == "nt", reason="the symlink flip is POSIX")
def test_a_reinstall_flips_and_retires_the_old_target(hooks_machine):
    host = hooks_machine()
    root = _install(host)
    first = _live(root).resolve()
    _install(host)
    assert _live(root).resolve() != first
    assert not first.exists()
    assert _ours(root) == sorted(["nxthooks", _live(root).resolve().name])


@pytest.mark.skipif(os.name == "nt", reason="the symlink flip is POSIX")
def test_a_real_directory_from_an_earlier_install_migrates_once(hooks_machine, monkeypatch):
    host = hooks_machine()
    monkeypatch.setattr(lifecycle, "_ATOMIC_LINK", False)
    root = _install(host)
    assert _live(root).is_dir() and not _live(root).is_symlink()
    monkeypatch.setattr(lifecycle, "_ATOMIC_LINK", True)

    _install(host)

    assert _live(root).is_symlink() and (_live(root) / "cli.py").is_file()
    assert _ours(root) == sorted(["nxthooks", _live(root).resolve().name])


@pytest.mark.parametrize("atomic", [True, False])
def test_the_runtime_is_importable_at_every_step(hooks_machine, monkeypatch, atomic):
    """Every rename and replace of the swap is observed. Around each one
    `lib/nxthooks` holds a full package, and the rescued source is already on
    disk in what is about to go live, never only in memory."""
    _layout(monkeypatch, atomic)
    host = hooks_machine()
    root, source = _seed_foreign(host, monkeypatch)
    live = _live(root)
    seen: list[bool] = []
    staged_rescue: list[bool] = []

    def watch(real):
        def step(src, dst, *a, **k):
            seen.append((live / "cli.py").is_file())
            if Path(dst) == live:
                staged_rescue.append(
                    (Path(src).resolve() / "adapters" / "nlm-session-start.py").is_file())
            result = real(src, dst, *a, **k)
            seen.append((live / "cli.py").is_file())
            return result
        return step

    monkeypatch.setattr(os, "replace", watch(os.replace))
    monkeypatch.setattr(os, "rename", watch(os.rename))

    _install(host)

    assert staged_rescue == [True]
    if atomic:
        assert seen and all(seen), seen
    assert "irreplaceable" in source.read_text()


@pytest.mark.parametrize("atomic", [True, False])
def test_a_failed_switch_restores_the_previous_runtime(hooks_machine, monkeypatch, atomic):
    _layout(monkeypatch, atomic)
    host = hooks_machine()
    root, source = _seed_foreign(host, monkeypatch)
    live = _live(root)
    before = (live / "cli.py").read_bytes()
    entries = _ours(root)
    refused: list[str] = []

    def refuse_once(real):
        def step(src, dst, *a, **k):
            if Path(dst) == live and not refused:
                refused.append(str(src))
                raise OSError("disk went away")
            return real(src, dst, *a, **k)
        return step

    monkeypatch.setattr(os, "replace", refuse_once(os.replace))
    monkeypatch.setattr(os, "rename", refuse_once(os.rename))

    with pytest.raises(OSError, match="disk went away"):
        _install(host)

    assert refused
    assert (live / "cli.py").read_bytes() == before
    assert "irreplaceable" in source.read_text()
    assert _ours(root) == entries


@pytest.mark.skipif(os.name == "nt", reason="the symlink flip is POSIX")
def test_a_crash_mid_migration_is_recovered_with_its_sources(hooks_machine, monkeypatch):
    """A process killed between moving the old directory aside and the flip
    leaves `lib/nxthooks` missing and `nxthooks.previous` holding the only copy
    of the rescued source. The next install puts it back rather than deleting
    it."""
    host = hooks_machine()
    root, source = _seed_foreign(host, monkeypatch)
    live = _live(root)
    real_dir = live.resolve()
    live.unlink()
    real_dir.rename(live.with_name("nxthooks.previous"))

    _install(host)

    assert (live / "cli.py").is_file()
    assert "irreplaceable" in source.read_text()
    assert _ours(root) == sorted(["nxthooks", live.resolve().name])


@pytest.mark.skipif(os.name == "nt", reason="the symlink flip is POSIX")
def test_a_crash_before_the_flip_leaves_a_working_runtime_and_is_swept(hooks_machine, monkeypatch):
    """Killed after staging, before the flip: the old target is still live and
    the orphaned staging dir is cleaned by the next install."""
    host = hooks_machine()
    root, source = _seed_foreign(host, monkeypatch)
    orphan = root / "lib" / ".nxthooks-0.0.0-dead"
    (orphan / "engines").mkdir(parents=True)
    (root / "lib" / ".nxthooks.link").symlink_to(orphan.name)
    assert (_live(root) / "cli.py").is_file()

    _install(host)

    assert not orphan.exists()
    assert not (root / "lib" / ".nxthooks.link").is_symlink()
    assert _ours(root) == sorted(["nxthooks", _live(root).resolve().name])


def test_uninstall_removes_the_link_and_the_versioned_dir(hooks_machine):
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    root = host.root()

    assert lifecycle.cmd_uninstall(_args(), host) == 0

    assert not root.exists(), sorted(str(p) for p in root.rglob("*"))


def test_the_installed_runtime_dispatches_through_the_link(hooks_machine):
    """The adapter puts `lib/` on sys.path and imports `nxthooks` through it."""
    import subprocess

    root = _install(hooks_machine())
    proc = subprocess.run(
        [sys.executable, "-P", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import nxthooks, nxthooks.cli; "
         "print(nxthooks.__file__)", str(root / "lib")],
        capture_output=True, text=True, timeout=60, env={"PATH": os.environ.get("PATH", "")})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith(str(root / "lib" / "nxthooks"))
