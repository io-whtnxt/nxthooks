import importlib.util
import subprocess
import sys

from nxthooks import platform
from nxthooks.registry import BUILTIN_DIR, builtin_manifests, render_collisions, resolve


def test_builtin_tier_ships_as_files_not_a_dict():
    assert BUILTIN_DIR.is_dir()
    assert list(BUILTIN_DIR.glob("*.toml")), "builtin hooks must ship as files"


def test_nxtlint_is_a_builtin():
    assert "nxtlint" in {e["name"] for e in builtin_manifests()}


def test_the_builtin_tier_ships_only_what_nxthooks_can_provide():
    """A builtin whose implementation NxtHooks cannot install is a promise it
    cannot keep. The git guards need NxtGit or nxtos-claims and stay in the
    library tier, which is NxtOS's to provide.

    This used to read `== {"nxtlint"}`, naming the answer instead of the
    property. It was correct when written -- nxtgrep could not ship, because
    its pattern pack lived outside any package -- and it stayed green after
    that stopped being true, so the builtin tier kept shipping one advisory
    linter and no security layer. Ask whether a writer exists, not whether
    the set matches a name.
    """
    for entry in builtin_manifests():
        module = entry.get("provision")
        assert module, f"builtin '{entry['name']}' declares no provision module"
        assert importlib.util.find_spec(module) is not None, (
            f"builtin '{entry['name']}' names provision module '{module}', "
            "which is not importable -- install would report a failure it "
            "cannot fix"
        )


def test_every_builtin_source_is_one_install_can_write(tmp_path, monkeypatch):
    """The property, exercised rather than pattern-matched on the path.

    Runs each declared writer against a relocated HOME and asserts the file
    the manifest points at is the file that appears. A writer aimed at the
    wrong component directory -- which is how both nxtlint and nxtgrep once
    wrote into NxtGit's `~/.nxtos/bin` -- fails here.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    for entry in builtin_manifests():
        proc = subprocess.run(
            [sys.executable, "-m", entry["provision"]],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, (
            f"{entry['provision']} failed: {proc.stderr or proc.stdout}"
        )
        source = entry["source"]
        assert source.startswith("~/"), source
        assert (tmp_path / source[2:]).is_file(), (
            f"builtin '{entry['name']}' points at {source}, which "
            f"{entry['provision']} did not write"
        )


def test_every_builtin_validates():
    for entry in builtin_manifests():
        assert entry["name"]
        assert entry["events"]
        assert entry["source"]


def _write(directory, name, source="/tmp/x", events='["session_start"]'):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.toml").write_text(
        f'[[hook]]\nname = "{name}"\nevents = {events}\nsource = "{source}"\n',
        encoding="utf-8",
    )


def test_user_replaces_builtin(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "unused"))
    _write(tmp_path / "cfg" / "nxt" / "hooks" / "hooks.d", "nxtlint", source="/tmp/mine")

    entries, collisions = resolve()

    nxtlint = [e for e in entries if e["name"] == "nxtlint"]
    assert len(nxtlint) == 1
    assert nxtlint[0]["source"] == "/tmp/mine"
    assert nxtlint[0]["tier"] == "user"
    # Deliberate asymmetry, pinned so it stays deliberate: user-over-library is
    # a reported collision, user-over-builtin is a silent override — that is
    # what an override is for. Every machine seeded by a pre-tier install holds
    # eight of these, and emitting them here would make the collision channel
    # permanent noise. `list`'s TIER column is where they surface instead.
    assert collisions == ()


def test_user_may_not_replace_a_library_hook(tmp_path, monkeypatch):
    """Library beats user, and that is the one place precedence inverts.

    The release spec says "builtin < library < user, the higher tier wins" in
    one line and "a user entry cannot silently replace a library entry" four
    lines later. The code implements the second, and should: a library hook is
    administered software, `hooks.d/` is an ordinary writable directory, and a
    security hook whose implementation can be swapped by dropping a file next
    to it is not a security hook.

    Not hypothetical. On the machine this was written on, a stale user
    `secret-scan` manifest pointed at `~/.nxtos/bin/nxtgrep-cc-hook`, the
    retired Claude-Code-only adapter, months after the harness-agnostic hook
    replaced it. Under user-wins, every secret scan on that machine would
    have been running the dead adapter. The collision warning is what says so.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _write(tmp_path / "cfg" / "nxt" / "hooks" / "hooks.d", "shared", source="/tmp/user")
    _write(tmp_path / "lib" / "hooks.d", "shared", source="/tmp/library")

    entries, collisions = resolve(library=tmp_path / "lib")

    kept = [e for e in entries if e["name"] == "shared"]
    assert [e["source"] for e in kept] == ["/tmp/library"]
    assert [(c.name, c.tier, c.kept_by) for c in collisions] == [
        ("shared", "user", "library")
    ]


def test_a_losing_hook_is_reported_never_dropped_silently(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _write(tmp_path / "cfg" / "nxt" / "hooks" / "hooks.d", "shared", source="/tmp/user")
    _write(tmp_path / "lib" / "hooks.d", "shared", source="/tmp/library")

    _, collisions = resolve(library=tmp_path / "lib")

    assert collisions, "a collision an operator cannot see got resolved by accident"


def test_dispatch_sees_builtin_hooks_with_no_user_config(tmp_path, monkeypatch):
    """The headline capability: a machine with an empty config still has hooks,
    because they ship with the package instead of being copied at install."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "unused"))

    from nxthooks.manifest import load_manifests

    assert "nxtlint" in {e["name"] for e in load_manifests()}


def test_verify_reports_a_collision(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _write(tmp_path / "cfg" / "nxt" / "hooks" / "hooks.d", "shared", source="/tmp/u")
    _write(tmp_path / "lib" / "hooks.d", "shared", source="/tmp/l")

    _, collisions = resolve(library=tmp_path / "lib")
    render_collisions(collisions)

    out = capsys.readouterr().out
    assert "shared" in out and "library" in out


def test_library_dir_follows_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert str(tmp_path) in str(platform.library_dir())


def test_resolve_reads_the_library_tier_by_default(tmp_path, monkeypatch):
    """The tier exists at runtime, not only when a test passes a path."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _write(platform.library_dir() / "hooks.d", "fleetwide", source="/tmp/fleet")

    entries, _ = resolve()

    assert "fleetwide" in {e["name"] for e in entries}
    assert next(e for e in entries if e["name"] == "fleetwide")["tier"] == "library"


def test_a_user_hook_still_loses_to_the_library_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _write(platform.library_dir() / "hooks.d", "shared", source="/tmp/lib")
    _write(platform.hooks_d(), "shared", source="/tmp/user")

    entries, collisions = resolve()

    assert [e["source"] for e in entries if e["name"] == "shared"] == ["/tmp/lib"]
    assert [(c.name, c.tier, c.kept_by) for c in collisions] == [
        ("shared", "user", "library")
    ]


def test_a_collision_warning_says_what_to_do_about_it(tmp_path, monkeypatch, capsys):
    """"lost to library" names the fact and leaves the operator guessing.

    Every collision is one of two things -- a stale duplicate to delete, or a
    deliberate override that this tier does not grant -- and both are resolved
    by editing the same file. Saying which file makes the warning a task
    rather than a status, which matters because these persist across every
    session until someone acts on one.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _write(tmp_path / "cfg" / "nxt" / "hooks" / "hooks.d", "shared", source="/tmp/user")
    _write(tmp_path / "lib" / "hooks.d", "shared", source="/tmp/library")

    _, collisions = resolve(library=tmp_path / "lib")
    render_collisions(collisions)

    out = capsys.readouterr().out
    assert "shared" in out
    assert "hooks.d/shared.toml" in out, (
        "the warning must name the file to edit, not only the hook: " + out
    )


def test_only_one_place_renders_a_collision():
    """`verify` printed its own copy of the format string.

    So improving the warning in `render_collisions` changed nothing an
    operator ever saw -- the same shadowing that let a corrected capability
    matrix sit in a module-level constant a second assignment had already
    overwritten. One renderer, asserted, because the duplicate is invisible
    until someone edits the wrong one.
    """
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[1] / "nxthooks"
    printers = [
        f.name for f in pkg.glob("*.py")
        if "lost to" in f.read_text() and f.name != "registry.py"
    ]
    assert printers == [], (
        f"{printers} format a collision warning instead of calling "
        "registry.render_collisions"
    )
