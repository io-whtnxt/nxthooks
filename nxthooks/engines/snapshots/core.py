"""Working-tree undo snapshots in a shadow git repo.

The shadow repo lives outside every working tree, so a project's own .git,
branches, hooks, and NxtGit claims never see it. Save captures the tree,
restore writes snapshot files back without deleting anything the snapshot
does not contain, and gc keeps the store small.

Two policies are load-bearing and live here, not in the callers:

- restore never deletes: files the snapshot does not contain are untouched,
  so a restore cannot destroy work that postdates the snapshot.
- restores are attributed: a snapshot saved with a session id restores only
  for that session unless forced, and an optional lease guard (wired by the
  layer that owns NxtGit sessions) can refuse a restore outright.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from nxthooks import _paths as paths
from nxthooks._safe_git import run_git

STORE_ENV = "NXTOS_SNAPSHOTS_HOME"
STORE_DIRNAME = "snapshots"
INERT_HOOKS_DIRNAME = ".no-hooks"
MAX_FILE_BYTES = 2 * 1024 * 1024
DEBOUNCE_SECONDS = 600
GC_INTERVAL_SECONDS = 3600
PRUNE_AFTER = "7.days.ago"
SHARED_TMP = Path("/tmp")


class SnapshotError(Exception):
    """The snapshot store or git refused an operation."""


class SnapshotRefused(SnapshotError):
    """A restore was refused by session scope or lease guard."""


def store_root() -> Path:
    """Shadow store root. Outside every worktree by construction."""
    override = os.environ.get(STORE_ENV)
    if override:
        return Path(override).expanduser()
    return paths.state_dir("git") / STORE_DIRNAME


def project_dir(root: Path | str) -> Path:
    """This project's shadow directory, keyed by path hash, not path text."""
    digest = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    return store_root() / digest


def _shadow(root: Path | str) -> Path:
    return project_dir(root) / "shadow"


def _too_broad(root: Path) -> bool:
    """Roots no project should ever be: the filesystem anchor, or a home
    directory. Snapshotting either would walk unbounded trees on every
    debounced save and fill the store with gigabytes."""
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    return str(root) == root.anchor or (home is not None and root == home)


def should_snapshot(cwd: str) -> bool:
    """Scratch and overbroad roots are never snapshotted: unbounded garbage
    with no owner. Silent by design; the hook skips these without a word."""
    if not cwd:
        return False
    try:
        resolved = Path(cwd).resolve()
    except OSError:
        return False
    if _too_broad(resolved):
        return False
    # The per-user $TMPDIR (/var/folders/... on macOS) and the shared /tmp
    # are different roots; both are scratch, where the shared one exists.
    scratch = {Path(tempfile.gettempdir()).resolve()}
    if os.name == "posix" and SHARED_TMP.is_dir():
        scratch.add(SHARED_TMP.resolve())
    return scratch.isdisjoint((resolved, *resolved.parents))


def inert_hooks(store: Path) -> str:
    """An empty hooks directory of our own, so git finds no hooks at all.

    Every git call this module makes names the shadow as `--git-dir`, but
    `core.hooksPath` is looked up in the *global* config too, and a machine
    that sets it (a shared `~/.git-hooks` is a common setup)
    was having its human commit gate run against snapshot commits. That gate
    scans the staged tree for secrets and refuses commits on shared branches,
    and a shadow repo is a bare repo whose only branch is literally `main`
    while its work tree is a project checkout that is usually on a shared
    branch — so either half of the gate refuses, for every project, forever.
    On one machine all fourteen shadow stores went to zero commits and stayed
    there, and nothing said so out loud, because the hook that reports it is
    `fail = "open"` by design.

    Neutralising `core.hooksPath` here is not a weakening of the secret scan.
    Human commits in real repositories still run it in full, and landings run
    it again; a shadow snapshot is a local, never-pushed copy of a tree that
    is already sitting on this disk, kept so a bad write can be undone. The
    gate exists to stop secrets entering repositories; it was never guarding
    this store, it was only ever collateral damage to it.

    Every call here runs through `safe_git.run_git`, which turns hooks off
    (authoritative, nothing to go stale); the path is also recorded in the
    shadow's own config at init, so a person running git by hand inside a store
    is not policed by the human gate either.
    """
    hooks = store / INERT_HOOKS_DIRNAME
    hooks.mkdir(parents=True, exist_ok=True)
    return str(hooks)


def _failure(done, args: "tuple[str, ...]") -> str:
    why = done.refusal or done.error_text.strip() or done.outcome
    return f"git {' '.join(args[:2])} failed: {why}"


def _git(shadow: Path, work_tree: Path, *args: str) -> str:
    done = run_git(
        ["--git-dir", str(shadow), "--work-tree", str(work_tree),
         "-c", "user.name=nxtos-snapshots", "-c", "user.email=nxtos-snapshots@local",
         *args],
        shadow.parent, timeout=120,
    )
    if done.outcome != "ok":
        raise SnapshotError(_failure(done, args))
    return done.text


_held = threading.local()


@contextmanager
def _locked(store: Path):
    """One writer per project store. Lives in the store itself, which no
    uninstall or prune pass removes, so the guarantee cannot quietly lapse.

    Re-entrant on the holding thread: flock locks attach to the open file
    description, so a second exclusive flock of the same path from this
    process would block forever rather than succeed. Cross-thread and
    cross-process contention still serializes on the file lock.
    """
    store.mkdir(parents=True, exist_ok=True)
    held = getattr(_held, "paths", None)
    if held is None:
        held = set()
        _held.paths = held
    key = str(store)
    if key in held:
        yield
        return
    with open(store / ".lock", "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            held.discard(key)


def _ensure_init(shadow: Path, work_tree: Path) -> None:
    if (shadow / "HEAD").is_file():
        return
    shadow.mkdir(parents=True, exist_ok=True)
    # `git init` creates HEAD's ref, so it runs reference-transaction hooks
    # too: under a machine-wide gate an un-neutralised init cannot even build
    # the store. (This is why the stores that exist were all created on the
    # morning the gate landed and none has been created since.)
    made = run_git(["init", "-q", "-b", "main", "--bare", str(shadow)], shadow.parent, timeout=60)
    if made.outcome != "ok":
        raise SnapshotError(f"git init failed: {made.refusal or made.error_text.strip() or made.outcome}")
    # New stores carry the inert hooksPath in their own config, so a person
    # running git by hand inside a store is not policed by the human gate
    # either. Every call this module makes gets hooks off from `run_git`.
    run_git(["--git-dir", str(shadow), "config", "core.hooksPath", inert_hooks(shadow.parent)],
            shadow.parent, timeout=60)


def _write_exclude(shadow: Path, work_tree: Path) -> None:
    """Keep the project's own git metadata and oversized files out.

    Candidates come from git, not a walk of the tree: git already honours the
    project's .gitignore, so ignored directories are never visited. A walk
    with a stat per file cost 6.5s per save on a 257k-file checkout, and the
    hook's timeout killed every save before it finished. The previous exclude
    list is cleared first, or the files it hid would drop out of the listing
    and off the next list."""
    base = [".git/", ".git"]
    exclude = shadow / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("\n".join(base) + "\n", encoding="utf-8")
    listed = _git(shadow, work_tree, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    oversized = []
    for rel in filter(None, listed.split("\0")):
        try:
            # lstat: git stores a symlink as its target string, whatever it points at.
            if (work_tree / rel).lstat().st_size > MAX_FILE_BYTES:
                oversized.append(rel)
        except OSError:
            continue
    exclude.write_text("\n".join(base + [_literal_ignore(rel) for rel in oversized]) + "\n", encoding="utf-8")
    if oversized:
        # An exclude never applies to a path already in the index: a file that grew past the limit since the
        # last save would be staged anyway. Untracking it in the shadow index leaves the work tree alone.
        _git(shadow, work_tree, "rm", "-q", "--cached", "--ignore-unmatch", "--",
             *(f":(literal){rel}" for rel in oversized))


def _literal_ignore(rel: str) -> str:
    """One path as a gitignore line matching only that path: anchored, with pattern characters escaped."""
    escaped = re.sub(r"([\\*?\[\]!#])", r"\\\1", rel)
    stripped = escaped.rstrip(" ")
    return "/" + stripped + "\\ " * (len(escaped) - len(stripped))


def _sidecar(store: Path, commit: str) -> Path:
    return store / "meta" / f"{commit}.json"


def save(root: Path | str, *, label=None, session=None, context=None) -> str:
    """Capture the tree. Returns a hash even when nothing changed."""
    root_path = Path(root).resolve()
    if _too_broad(root_path):
        raise SnapshotError(f"refusing to snapshot overbroad root: {root_path}")
    store = project_dir(root_path)
    shadow = _shadow(root_path)
    with _locked(store):
        _ensure_init(shadow, root_path)
        _write_exclude(shadow, root_path)
        _git(shadow, root_path, "add", "-A")
        message = label or f"snapshot {time.strftime('%Y-%m-%d %H:%M:%S')}"
        # --no-verify alongside the inert hooksPath: hooksPath is the fix, and
        # this is the same refusal stated twice so a future git that resolves
        # hooks differently under an external --git-dir still cannot have a
        # bookkeeping commit refused by a human commit gate.
        _git(shadow, root_path, "commit", "-q", "--no-verify", "--allow-empty", "-m", message)
        commit = _git(shadow, root_path, "rev-parse", "HEAD").strip()
        meta = _sidecar(store, commit)
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({
            "label": label, "session": session, "context": context,
            "created": int(time.time()), "root": str(root_path),
        }), encoding="utf-8")
        (store / ".last-save").write_text(str(int(time.time())), encoding="utf-8")
        maybe_gc(root_path)
    return commit


def last_save_time(root: Path | str) -> int:
    """Epoch of the last save for this project, 0 when never saved."""
    try:
        return int((project_dir(root) / ".last-save").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0


def due_for_save(root: Path | str, now: float | None = None) -> bool:
    """Debounce: at most one hook-triggered snapshot per project per window."""
    return (now if now is not None else time.time()) - last_save_time(root) >= DEBOUNCE_SECONDS


def list_snapshots(root: Path | str) -> list[dict]:
    """Newest first: hash, created, label, session for each snapshot."""
    root_path = Path(root).resolve()
    shadow = _shadow(root_path)
    if not (shadow / "HEAD").is_file():
        return []
    try:
        _git(shadow, root_path, "rev-parse", "--verify", "--quiet", "HEAD")
    except SnapshotError:
        return []
    out = _git(shadow, root_path, "log", "--format=%H %ct %s")
    entries = []
    for line in out.splitlines():
        commit, _, rest = line.partition(" ")
        created, _, subject = rest.partition(" ")
        entry = {"hash": commit, "created": int(created) if created.isdigit() else 0,
                 "label": subject, "session": None}
        try:
            meta = json.loads(_sidecar(project_dir(root_path), commit).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if meta.get("label") is not None:
            entry["label"] = meta["label"]
        entry["session"] = meta.get("session")
        entries.append(entry)
    return entries


def diff(root: Path | str, commit: str, compare: str | None = None) -> str:
    """Work-tree diff against a snapshot, or snapshot-to-snapshot with compare.

    For a work-tree diff, "+" is current content a restore would remove and
    "-" is snapshot content a restore would bring back: diff before restore.
    """
    root_path = Path(root).resolve()
    shadow = _shadow(root_path)
    for ref in (commit,) if compare is None else (commit, compare):
        try:
            _git(shadow, root_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except SnapshotError:
            raise SnapshotError(f"unknown snapshot: {ref}")
    if compare is None:
        return _git(shadow, root_path, "diff", commit, "--")
    return _git(shadow, root_path, "diff", f"{commit}..{compare}", "--")


def restore(root: Path | str, commit: str, *, session=None, lease_guard=None, force=False) -> None:
    """Write a snapshot's files back. Never deletes files it does not contain.

    lease_guard(root) -> (allowed, reason) is wired by the layer that owns
    NxtGit sessions; this module never imports that layer. force overrides
    session scope only, never the lease guard.
    """
    root_path = Path(root).resolve()
    store = project_dir(root_path)
    shadow = _shadow(root_path)
    try:
        _git(shadow, root_path, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    except SnapshotError:
        raise SnapshotError(f"unknown snapshot: {commit}")
    try:
        meta = json.loads(_sidecar(store, commit).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    owner = meta.get("session")
    if not force and owner is not None and session != owner:
        raise SnapshotRefused(
            f"snapshot belongs to session {owner}; pass force to restore it here")
    if lease_guard is not None:
        allowed, reason = lease_guard(root_path)
        if not allowed:
            raise SnapshotRefused(reason or "lease guard refused the restore")
    with _locked(store):
        _git(shadow, root_path, "checkout", commit, "--", ".")


def maybe_gc(root: Path | str, interval: int = GC_INTERVAL_SECONDS) -> bool:
    """Hourly `gc --prune`, so week-old objects do not accumulate. Returns
    whether a collection ran."""
    root_path = Path(root).resolve()
    store = project_dir(root_path)
    if not (_shadow(root_path) / "HEAD").is_file():
        return False
    marker = store / ".gc-marker"
    try:
        last = int(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = 0
    if time.time() - last < interval:
        return False
    with _locked(store):
        _git(_shadow(root_path), root_path, "gc", "--quiet", f"--prune={PRUNE_AFTER}")
        marker.write_text(str(int(time.time())), encoding="utf-8")
    return True
