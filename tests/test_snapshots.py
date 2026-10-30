"""Shadow snapshots: save/list/diff/restore, scoping, and the hook shim.

The store is redirected per test (NXTOS_SNAPSHOTS_HOME), so no test touches
a real ~/.nxtos tree. Git is exercised for real: a mocked git would prove
nothing about the restore-never-deletes guarantee, which is the point.
"""
import io
import json
import subprocess
import sys
import tempfile
import threading
import tomllib
from pathlib import Path

import pytest

from nxthooks.engines.snapshots import core
from nxthooks.engines.snapshots.core import (
    SnapshotError,
    SnapshotRefused,
    diff,
    due_for_save,
    list_snapshots,
    project_dir,
    restore,
    save,
    should_snapshot,
    store_root,
)


@pytest.fixture(autouse=True)
def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("NXTOS_SNAPSHOTS_HOME", str(tmp_path / "store"))
    return tmp_path


def _project(tmp_path, name="proj"):
    root = tmp_path / name
    root.mkdir()
    (root / "a.txt").write_text("v1\n")
    return root


def _tracked(root):
    shadow = project_dir(root) / "shadow"
    out = subprocess.run(
        ["git", "--git-dir", str(shadow), "--work-tree", str(root),
         "ls-tree", "-r", "HEAD", "--name-only"],
        capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def test_save_list_and_diff_round_trip(tmp_path):
    root = _project(tmp_path)
    first = save(root, label="one", session="s1")
    (root / "a.txt").write_text("v2\n")
    second = save(root, label="two", session="s1")
    entries = list_snapshots(root)
    assert [e["hash"] for e in entries] == [second, first]
    assert entries[0]["label"] == "two" and entries[0]["session"] == "s1"
    worktree = diff(root, first)
    assert "-v1" in worktree and "+v2" in worktree
    between = diff(root, first, second)
    assert "-v1" in between and "+v2" in between


def test_save_returns_a_hash_when_nothing_changed(tmp_path):
    root = _project(tmp_path)
    assert save(root) and save(root)


def test_project_git_dir_and_oversized_files_are_excluded(tmp_path):
    root = _project(tmp_path)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x\n")
    (root / "big.bin").write_bytes(b"0" * (core.MAX_FILE_BYTES + 1))
    save(root)
    tracked = _tracked(root)
    assert "a.txt" in tracked
    assert ".git/config" not in tracked
    assert "big.bin" not in tracked


def test_restore_brings_back_snapshot_content_but_deletes_nothing(tmp_path):
    root = _project(tmp_path)
    snap = save(root)
    (root / "a.txt").write_text("broken\n")
    (root / "new.txt").write_text("postdates the snapshot\n")
    restore(root, snap)
    assert (root / "a.txt").read_text() == "v1\n"
    assert (root / "new.txt").read_text() == "postdates the snapshot\n"


def test_unknown_snapshot_fails_loud(tmp_path):
    root = _project(tmp_path)
    save(root)
    with pytest.raises(SnapshotError, match="unknown snapshot"):
        diff(root, "0" * 40)
    with pytest.raises(SnapshotError, match="unknown snapshot"):
        restore(root, "0" * 40)


def test_restore_is_scoped_to_the_saving_session(tmp_path):
    root = _project(tmp_path)
    snap = save(root, session="owner")
    (root / "a.txt").write_text("broken\n")
    with pytest.raises(SnapshotRefused, match="session owner"):
        restore(root, snap, session="stranger")
    restore(root, snap, session="owner")
    assert (root / "a.txt").read_text() == "v1\n"
    (root / "a.txt").write_text("broken\n")
    restore(root, snap, session="stranger", force=True)
    assert (root / "a.txt").read_text() == "v1\n"


def test_restore_honors_the_lease_guard_and_force_never_overrides_it(tmp_path):
    root = _project(tmp_path)
    snap = save(root)
    seen = []
    deny = lambda r: (seen.append(r) or (False, "write lease held elsewhere"))
    with pytest.raises(SnapshotRefused, match="write lease held elsewhere"):
        restore(root, snap, lease_guard=deny)
    assert seen and seen[0] == root.resolve()
    with pytest.raises(SnapshotRefused, match="write lease held elsewhere"):
        restore(root, snap, lease_guard=deny, force=True)
    restore(root, snap, lease_guard=lambda r: (True, ""))
    assert (root / "a.txt").read_text() == "v1\n"


def test_save_records_session_and_context_in_the_sidecar(tmp_path):
    root = _project(tmp_path)
    snap = save(root, label="l", session="s9", context="claim c1")
    meta = json.loads((project_dir(root) / "meta" / f"{snap}.json").read_text())
    assert (meta["session"], meta["context"], meta["label"]) == ("s9", "claim c1", "l")


def test_debounce_and_scratch_policy(tmp_path, monkeypatch):
    root = _project(tmp_path)
    assert due_for_save(root) is True
    save(root)
    assert due_for_save(root) is False
    assert should_snapshot("") is False
    assert should_snapshot(str(tmp_path)) is False  # pytest tmp lives under system tmp
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/nonexistent-tmp")
    monkeypatch.setattr(core, "SHARED_TMP", Path("/nonexistent-shared-tmp"))  # Linux pytest tmp is under /tmp
    assert should_snapshot(str(root)) is True


def test_overbroad_roots_are_skipped_silently_and_refused_loudly(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert should_snapshot(str(fake_home)) is False
    assert should_snapshot("/") is False
    with pytest.raises(SnapshotError, match="overbroad root"):
        save(fake_home)
    with pytest.raises(SnapshotError, match="overbroad root"):
        save("/")


def test_parallel_saves_serialize_without_corruption(tmp_path):
    root = _project(tmp_path)
    (root / "a.txt").write_text("v1\n")
    hashes = []
    threads = [threading.Thread(target=lambda: hashes.append(save(root))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(hashes) == 5
    assert len(list_snapshots(root)) == 5


def test_gc_runs_on_interval_and_keeps_snapshots(tmp_path):
    root = _project(tmp_path)
    snap = save(root)
    assert core.maybe_gc(root, interval=0) is True
    assert core.maybe_gc(root) is False
    assert [e["hash"] for e in list_snapshots(root)] == [snap]


def test_hook_saves_silently_on_a_write_and_skips_scratch(monkeypatch, tmp_path, capsys):
    from nxthooks.engines.snapshots import hook as hook_mod
    monkeypatch.setattr(hook_mod, "should_snapshot", lambda cwd: True)
    root = _project(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(root)})))
    monkeypatch.setenv("NXTHOOKS_TOOL", "Write")
    assert hook_mod.main() == 0
    assert capsys.readouterr().out == ""
    assert len(list_snapshots(root)) == 1
    # Second write inside the window: debounced, still silent.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(root)})))
    assert hook_mod.main() == 0
    assert len(list_snapshots(root)) == 1


def test_hook_skips_scratch_and_fails_open_on_bad_input(monkeypatch, tmp_path, capsys):
    from nxthooks.engines.snapshots import hook as hook_mod
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    assert hook_mod.main() == 0  # tmp cwd: skipped silently
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hook_mod.main() == 1
    assert "could not parse" in capsys.readouterr().err


def test_install_writes_an_executable_shim(tmp_path):
    from nxthooks.engines.snapshots.install import SHIM_NAME, install_shim
    target = install_shim(tmp_path / "bin")
    assert target == tmp_path / "bin" / SHIM_NAME
    text = target.read_text()
    assert "from nxthooks.engines.snapshots.hook import main" in text
    assert "nxthooks" in text  # baked package root
    import os
    assert os.access(target, os.X_OK)


def test_builtin_manifest_declares_the_snapshot_hook():
    manifest = Path(__file__).parents[1] / "nxthooks" / "builtin" / "snapshot-save.toml"
    entry = tomllib.loads(manifest.read_text())["hook"][0]
    assert entry["name"] == "snapshot-save"
    assert entry["events"] == ["before_tool"]
    assert entry["matchers"] == ["Write", "Edit", "MultiEdit", "NotebookEdit"]
    assert entry["source"] == "~/.local/state/nxt/git/snapshots/bin/snapshot-save-hook.py"
    assert entry["provision"] == "nxthooks.engines.snapshots.install"
    assert entry["policy"] == "advise" and entry["fail"] == "open"


def test_store_root_prefers_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NXTOS_SNAPSHOTS_HOME", str(tmp_path / "custom"))
    assert store_root() == tmp_path / "custom"


# --- the shadow store answers to no human commit gate -------------------
#
# `core.hooksPath` set machine-wide (the real fleet has ~/.git-hooks: a secret
# scan and a shared-branch guard for human commits) used to run against every
# snapshot commit. The shadow is a bare repo whose only branch is `main`, so
# both halves of that gate refuse, for every project, forever -- and the hook
# that would have said so is `fail = "open"`. Git runs for real here for the
# same reason it does everywhere else in this file.

_GATE_BODY = "#!/bin/sh\necho 'gate: refused' >&2\nexit 1\n"


def _machine_wide_gate(monkeypatch, tmp_path, body=_GATE_BODY):
    """Install a core.hooksPath the way a fleet does, via the global gitconfig."""
    hooks = tmp_path / "git-hooks"
    hooks.mkdir()
    for name in ("pre-commit", "reference-transaction"):
        gate = hooks / name
        gate.write_text(body)
        gate.chmod(0o755)
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[core]\n\thooksPath = {hooks}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    return hooks


def test_save_survives_a_machine_wide_commit_gate(tmp_path, monkeypatch):
    root = _project(tmp_path)
    _machine_wide_gate(monkeypatch, tmp_path)
    commit = save(root, label="under the gate")
    assert commit
    assert list_snapshots(root)[0]["label"] == "under the gate"
    assert (root / "a.txt").read_text() == "v1\n"


def test_repeated_saves_still_work_under_the_gate(tmp_path, monkeypatch):
    """One lucky commit is not the fix; the safety net has to keep working."""
    root = _project(tmp_path)
    _machine_wide_gate(monkeypatch, tmp_path)
    for n in range(3):
        (root / "a.txt").write_text(f"v{n}\n")
        save(root, label=f"v{n}")
    assert [s["label"] for s in list_snapshots(root)][:3] == ["v2", "v1", "v0"]


def test_the_gate_bites_an_ordinary_repo(monkeypatch, tmp_path):
    """Control: the fixture above really is a machine-wide gate.

    Without it, the tests that pass could be passing because the gate was never
    installed -- which is how a fix gets written down as proven. `git init` even
    creates a ref, so the gate refuses ordinary repository creation."""
    _machine_wide_gate(monkeypatch, tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = subprocess.run(["git", "init", "-q", "--bare", str(plain / ".git")],
                          capture_output=True, text=True, check=False)
    assert proc.returncode != 0
    assert "gate: refused" in proc.stderr


def test_stores_created_before_the_fix_still_save(monkeypatch, tmp_path):
    """Every store on the fleet predates the inert hooksPath in its config.

    The per-call `-c` is what rescues them, so it is the half that must not
    regress: drop the config entry the way a legacy store lacks it, and saving
    still has to work."""
    root = _project(tmp_path)
    _machine_wide_gate(monkeypatch, tmp_path)
    save(root, label="first")
    shadow = core._shadow(root)
    subprocess.run(["git", "--git-dir", str(shadow), "config", "--unset", "core.hooksPath"],
                   check=True)
    resolved = subprocess.run(["git", "--git-dir", str(shadow), "config", "--get", "core.hooksPath"],
                              capture_output=True, text=True).stdout.strip()
    assert resolved.endswith("git-hooks")  # the store now resolves to the human gate
    (root / "a.txt").write_text("v2\n")
    save(root, label="second")
    assert [s["label"] for s in list_snapshots(root)][:2] == ["second", "first"]


def test_new_store_records_the_inert_hooks_path(tmp_path):
    """So a person running git by hand inside a store is not policed either."""
    root = _project(tmp_path)
    save(root)
    shadow = core._shadow(root)
    answered = subprocess.run(
        ["git", "--git-dir", str(shadow), "config", "--get", "core.hooksPath"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert answered.endswith(core.INERT_HOOKS_DIRNAME)
    assert Path(answered).is_dir()


def test_save_asks_git_for_the_file_list_instead_of_walking_the_tree(tmp_path, monkeypatch):
    """A full os.walk + stat of the work tree cost 6.5s per save on a 257k-file
    checkout, mostly inside directories the project ignores, so the 15s hook
    timeout killed the save on every write and it never once completed."""
    root = _project(tmp_path)
    monkeypatch.setattr(core.os, "walk", lambda *a, **k: (_ for _ in ()).throw(AssertionError("walked the tree")))
    save(root)
    assert "a.txt" in _tracked(root)


def test_oversized_files_stay_excluded_on_every_save_and_at_any_depth(tmp_path):
    root = _project(tmp_path)
    (root / "sub").mkdir()
    (root / "sub" / "big.bin").write_bytes(b"0" * (core.MAX_FILE_BYTES + 1))
    (root / "big.bin").write_bytes(b"0" * (core.MAX_FILE_BYTES + 1))
    save(root)
    (root / "a.txt").write_text("v2\n")
    save(root)
    tracked = _tracked(root)
    assert "a.txt" in tracked and "big.bin" not in tracked and "sub/big.bin" not in tracked


def test_the_shared_tmp_is_scratch_as_well_as_the_per_user_one(monkeypatch):
    """macOS gives each user a $TMPDIR under /var/folders; /tmp (/private/tmp) is a different, shared scratch root."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/nonexistent-tmp")
    assert should_snapshot("/tmp") is False
    assert should_snapshot(str(Path("/tmp").resolve())) is False


def test_a_file_that_grows_past_the_limit_leaves_the_store(tmp_path):
    root = _project(tmp_path)
    (root / "grows.bin").write_bytes(b"0" * 10)
    save(root)
    assert "grows.bin" in _tracked(root)
    (root / "grows.bin").write_bytes(b"0" * (core.MAX_FILE_BYTES + 1))
    save(root)
    assert "grows.bin" not in _tracked(root) and (root / "grows.bin").exists()


def test_oversized_names_are_excluded_literally_and_only_where_they_are(tmp_path):
    root = _project(tmp_path)
    big = b"0" * (core.MAX_FILE_BYTES + 1)
    for name in ("#hash.bin", "!bang.bin", "star*.bin", "big.bin"):
        (root / name).write_bytes(big)
    (root / "starX.bin").write_text("small\n")          # would match an unescaped star*.bin
    (root / "sub").mkdir()
    (root / "sub" / "big.bin").write_text("small\n")    # same name, small: an unanchored pattern would drop it
    save(root)
    tracked = _tracked(root)
    assert not {"#hash.bin", "!bang.bin", "star*.bin", "big.bin"} & set(tracked)
    assert {"starX.bin", "sub/big.bin", "a.txt"} <= set(tracked)


def test_save_lists_the_whole_tree_whatever_the_callers_cwd(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / "sub").mkdir()
    (root / "big.bin").write_bytes(b"0" * (core.MAX_FILE_BYTES + 1))
    monkeypatch.chdir(root / "sub")
    save(root)
    assert "big.bin" not in _tracked(root)


def test_a_symlink_to_a_large_file_is_kept(tmp_path):
    root = _project(tmp_path)
    target = tmp_path / "large.dat"; target.write_bytes(b"0" * (core.MAX_FILE_BYTES + 1))
    (root / "link.dat").symlink_to(target)
    save(root)
    assert "link.dat" in _tracked(root)
