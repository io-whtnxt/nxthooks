"""Every path NxtOS owns, derived in one place.

NxtOS imposes a four-role ontology on its components and does not obey it
itself. Counting the host's own state the family is at eight home-relative
roots: `~/.config/nxt/<c>`, `~/.local/share/nxt/<c>`, `~/.local/state/nxt/<c>`,
`~/.nxtos/{bin,mcp,claims.db,token,workspace,keybindings.json,preferences.json}`,
`~/Library/Application Support/NxtOS/{plugins,agents,hooks,properties,nlm,
<workspace-key>/}`, `~/.nxtskills/actions`, `~/.nxtcron/`, and
`<workspace>/nxtos/`. Five of those App Support subpaths are spelled with no
platform guard at all, so a Linux operator's user plugin root is literally
`~/Library/Application Support/NxtOS/plugins`.

Eight roots is not eight decisions. It is one decision made eight times by
eight modules that each had a path to build and no owner to call. This module
is that owner. The durable half of the fix is not this file, it is
`tests/test_paths.py`, which walks the shipped packages and fails when a ninth
root appears.

The convention, normative per N1 of the uniformity plan:

    role     POSIX                                 env override
    config   $XDG_CONFIG_HOME/nxt/<id>             NXT_CONFIG_HOME
    data     $XDG_DATA_HOME/nxt/<id>               NXT_DATA_HOME
    state    $XDG_STATE_HOME/nxt/<id>              NXT_STATE_HOME
    cache    $XDG_CACHE_HOME/nxt/<id>              NXT_CACHE_HOME

macOS follows POSIX. It does not get `~/Library/Application Support`. The two
components that already conform chose XDG on macOS, and a rule with a
per-platform fork for the host alone is the drift this module exists to end.

Windows: config under `%APPDATA%\\nxt\\<id>`, the other three under
`%LOCALAPPDATA%\\nxt\\<id>\\{data,state,cache}`. Windows is out of scope for
v1, but the functions answer for it anyway, so "does this run on Windows" is a
test rather than a grep.

The `NXT_*_HOME` overrides replace the whole `nxt/` root, which is why
`config_home()` and its siblings return that root and not the XDG base above
it. One seam, set once, relocates the entire family: that is what makes a
sandboxed test HOME and a packaged install the same code path.

Adoption (N1-b) is per path on first resolve. `resolve(component, role,
relative)` is the one function every call site uses: it answers with the
normative path, and the first time in a process that a given path is asked
for, it moves whatever the operator has at the matching legacy location onto
it. The move is staged and then renamed, never trusted halfway, and it leaves
a breadcrumb behind so a second run can say the move already happened rather
than doing it again.

`tests/test_paths.py` carries the drainable list of call sites that still
spell a root for themselves, and fails the moment one is flipped without its
entry being deleted.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows, out of scope for v1
    fcntl = None

_LOG = logging.getLogger(__name__)

WINDOWS = os.name == "nt"
DARWIN = sys.platform == "darwin"

ROOT_NAME = "nxt"

#: The `nxt.components` entry-point name, and the directory name under every
#: role root. Fixed: a component id is not a free-text label.
#:
#: The plan's family list also names `cron` and `vision`. Neither ships from
#: this repository (NxtCron is its own tree, and there is no vision component
#: yet), so neither is accepted here. A component id this module does not know
#: is a typo until the component exists, and a typo that resolves is how the
#: ninth root gets created.
COMPONENT_IDS = ("os", "hooks", "skills", "wiki", "mcp", "git", "grep", "lint", "agent")

ROLES = ("config", "data", "state", "cache")

_POSIX_BASES = {
    "config": ("XDG_CONFIG_HOME", (".config",)),
    "data": ("XDG_DATA_HOME", (".local", "share")),
    "state": ("XDG_STATE_HOME", (".local", "state")),
    "cache": ("XDG_CACHE_HOME", (".cache",)),
}

_WINDOWS_BASES = {
    "config": ("APPDATA", ("AppData", "Roaming")),
    "data": ("LOCALAPPDATA", ("AppData", "Local")),
    "state": ("LOCALAPPDATA", ("AppData", "Local")),
    "cache": ("LOCALAPPDATA", ("AppData", "Local")),
}


class UnknownComponent(ValueError):
    """Raised for a component id outside `COMPONENT_IDS`."""


def _home(home: Path | str | None) -> Path:
    return Path.home() if home is None else Path(home)


#: `NXTOS_STATE_HOME` named the host's state root before N1 and is honoured as
#: an alias for `NXT_STATE_HOME` through 0.4, per N1-c. It is an alias for the
#: *new* meaning: the whole `nxt/` root, not the per-workspace parent it used
#: to name. The old meaning is still read, once, by `_legacy_state_home` when
#: deciding where an operator's existing workspace state is to be adopted from.
_STATE_HOME_ALIAS = "NXTOS_STATE_HOME"


def _root(role: str, env: dict | None, home: Path | str | None) -> Path:
    environ = os.environ if env is None else env

    override = (environ.get(f"NXT_{role.upper()}_HOME") or "").strip()
    if not override and role == "state":
        override = (environ.get(_STATE_HOME_ALIAS) or "").strip()
    if override:
        # The override *is* the nxt root. Appending ROOT_NAME to it would make
        # NXT_STATE_HOME=/tmp/x resolve to /tmp/x/nxt, which is not what an
        # operator pointing at a directory means by pointing at it.
        return Path(override).expanduser()

    var, parts = (_WINDOWS_BASES if WINDOWS else _POSIX_BASES)[role]
    raw = (environ.get(var) or "").strip()
    base = Path(raw).expanduser() if raw else _home(home).joinpath(*parts)
    return base / ROOT_NAME


def config_home(env: dict | None = None, home: Path | str | None = None) -> Path:
    """The `nxt/` config root, not the XDG base above it."""
    return _root("config", env, home)


def data_home(env: dict | None = None, home: Path | str | None = None) -> Path:
    return _root("data", env, home)


def state_home(env: dict | None = None, home: Path | str | None = None) -> Path:
    return _root("state", env, home)


def cache_home(env: dict | None = None, home: Path | str | None = None) -> Path:
    return _root("cache", env, home)


def _component_dir(
    role: str, component: str, env: dict | None, home: Path | str | None
) -> Path:
    if component not in COMPONENT_IDS:
        raise UnknownComponent(
            f"{component!r} is not a component id; known ids are "
            f"{', '.join(COMPONENT_IDS)}"
        )
    root = _root(role, env, home)
    if WINDOWS and role != "config":
        return root / component / role
    return root / component


def config_dir(
    component: str, env: dict | None = None, home: Path | str | None = None
) -> Path:
    return _component_dir("config", component, env, home)


def data_dir(
    component: str, env: dict | None = None, home: Path | str | None = None
) -> Path:
    return _component_dir("data", component, env, home)


def state_dir(
    component: str, env: dict | None = None, home: Path | str | None = None
) -> Path:
    return _component_dir("state", component, env, home)


def cache_dir(
    component: str, env: dict | None = None, home: Path | str | None = None
) -> Path:
    return _component_dir("cache", component, env, home)


# ---------------------------------------------------------------------------
# Workspace keys
# ---------------------------------------------------------------------------

#: What a directory name under the host's per-workspace state root looks like.
#: Adoption needs it to tell a workspace key from its siblings: the legacy App
#: Support root holds `plugins`, `agents`, `hooks`, `properties`, `nlm` and
#: `nxtgrep` beside the workspace keys, and those move somewhere else entirely.
WORKSPACE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+-[0-9a-f]{12}$")


def workspace_key(workspace: Path | str) -> str:
    """A stable, collision-free directory name for one workspace.

    The readable half is for a human opening the state directory; the digest
    is what actually makes it unique, because two workspaces called `site`
    under different parents are not the same workspace.
    """
    resolved = Path(workspace).expanduser().resolve()
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved.name).strip("-") or "workspace"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"{label}-{digest}"


# ---------------------------------------------------------------------------
# Where things used to live
# ---------------------------------------------------------------------------

#: Substituted, per resolve, by the `workspace_key` of the workspace asked for.
KEY_TOKEN = "<key>"

_APP_SUPPORT = "~/Library/Application Support/NxtOS"


@dataclass(frozen=True)
class Legacy:
    """One pre-N1 location and the normative path it maps onto.

    `relative` is relative to `<role>_dir(component)`; empty means the
    component's role directory itself. `posix` is the non-darwin spelling
    where the two differed; empty means the one in `path` was used on every
    platform, which for the App Support entries is the bug, not a shorthand.

    `by_command` marks a path `resolve` must never adopt: it moves on an
    explicit operator command instead, because something else depends on
    knowing exactly when it moved.
    """

    role: str
    relative: str
    path: str
    posix: str = ""
    by_command: bool = False

    def legacy_path(
        self,
        home: Path | str | None = None,
        darwin: bool = True,
        key: str = "",
    ) -> Path:
        spelling = self.path if (darwin or not self.posix) else self.posix
        if key:
            spelling = spelling.replace(KEY_TOKEN, key)
        return Path(spelling.replace("~", str(_home(home)), 1))

    def target(
        self,
        component: str,
        env: dict | None = None,
        home: Path | str | None = None,
        key: str = "",
    ) -> Path:
        base = _component_dir(self.role, component, env, home)
        relative = self.relative.replace(KEY_TOKEN, key) if key else self.relative
        return base / relative if relative else base


#: Read only by `resolve` and `adopt`, reported by `breadcrumbs()` from 0.3.0,
#: and deleted in 0.5.0. Roots this repository does not own (`~/.nxtcron/`) are
#: not listed; `mcp` and `git` are listed because the host resolves them even
#: though their runtimes are installed from elsewhere.
LEGACY: dict[str, tuple[Legacy, ...]] = {
    "os": (
        Legacy("state", "token", "~/.nxtos/token"),
        Legacy("config", "workspace", "~/.nxtos/workspace"),
        Legacy("config", "preferences.json", "~/.nxtos/preferences.json"),
        Legacy("config", "keybindings.json", "~/.nxtos/keybindings.json"),
        Legacy("config", "plugin-preferences.json",
               f"{_APP_SUPPORT}/plugin-preferences.json"),
        Legacy("data", "plugins", f"{_APP_SUPPORT}/plugins"),
        Legacy("data", "agents", f"{_APP_SUPPORT}/agents"),
        Legacy("data", "properties", f"{_APP_SUPPORT}/properties"),
        Legacy("data", "hooks", f"{_APP_SUPPORT}/hooks"),
        Legacy("data", "nlm", f"{_APP_SUPPORT}/nlm"),
        Legacy("state", f"workspaces/{KEY_TOKEN}", f"{_APP_SUPPORT}/{KEY_TOKEN}",
               posix=f"~/.local/state/nxtos/{KEY_TOKEN}"),
    ),
    "lint": (
        # The shim shipped here because ~/.nxtos/bin was where the guards
        # lived. That root belongs to `git`, so NxtLint was writing into a
        # neighbour tree; a manifest pointing at the old path keeps resolving
        # until adoption moves it.
        Legacy("data", "bin/nxtlint-hook.py", "~/.nxtos/bin/nxtlint-hook.py"),
    ),
    "grep": (
        Legacy("config", "patterns.conf", f"{_APP_SUPPORT}/nxtgrep/patterns.conf"),
        # The component id is `grep`; `nxtgrep` was the directory NxtGrep's
        # own config chose. Not in the plan's mapping table, which recorded
        # only the App Support half, but leaving it would put one component's
        # two config files in two directories -- the config in `nxt/nxtgrep/`
        # and the pattern pack the line above moves into `nxt/grep/`.
        Legacy("config", "config.toml", "~/.config/nxt/nxtgrep/config.toml"),
    ),
    "skills": (
        Legacy("state", "actions", "~/.nxtskills/actions"),
    ),
    "mcp": (
        Legacy("data", "", "~/.nxtos/mcp"),
    ),
    "git": (
        Legacy("data", "bin", "~/.nxtos/bin"),
        # Moves last, by an explicit `nxtos-claims migrate`, never on a
        # resolve: a claim database adopted out from under a live claim is a
        # guard that stops guarding while every process still thinks it holds
        # the tree.
        Legacy("state", "claims.db", "~/.nxtos/claims.db", by_command=True),
    ),
}


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------

#: A finished copy waiting on one atomic rename. Never read as content.
_ADOPTING_SUFFIX = ".adopting"
#: A copy in progress. Never trusted, always discarded.
_COPYING_SUFFIX = ".copying"

#: Left beside a legacy location once its content has moved, naming where it
#: went. The plan says "a breadcrumb at the legacy location named MIGRATED-TO";
#: it is spelled as a sibling rather than a child because more than half of
#: `LEGACY` is files (`token`, `preferences.json`, `patterns.conf`,
#: `claims.db`), and a file has no inside to put a marker in. One spelling also
#: means one glob finds every breadcrumb on the machine.
BREADCRUMB_SUFFIX = ".MIGRATED-TO"

#: WAL and shared-memory files travel with the database they belong to. A -wal
#: holding committed pages left behind while its database moves is data loss.
_SQLITE_SIDECARS = ("-wal", "-shm")

_adopted: set[str] = set()
_adopt_thread_lock = threading.Lock()


def breadcrumb_path(legacy: Path) -> Path:
    return legacy.parent / f"{legacy.name}{BREADCRUMB_SUFFIX}"


def _discard(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


#: One lock file for the whole family, under the state root. Not one per path:
#: a lock file cannot be removed safely while another process may still hold a
#: descriptor on it, so anything created here is permanent, and per-path locks
#: would leave a hidden file beside every root NxtOS owns forever. Adoption is
#: a once-per-install event, so serialising all of it costs nothing.
_LOCK_NAME = ".adopt.lock"


@contextmanager
def _cross_process_lock(lock_root: Path):
    """Serialise adoption across every NxtOS process.

    The desktop app, `nxtos-serve` and a CLI invocation can all resolve the
    same path within milliseconds of each other on login. The staged rename
    below is already atomic per step, but two processes racing on the same
    staging name would have one of them copying into the other's `.copying`.

    On a platform without `fcntl` this degrades to no lock. Windows is out of
    scope for v1, and a degraded lock is still ordered by the renames.
    """
    if fcntl is None:  # pragma: no cover - Windows, out of scope for v1
        yield
        return
    try:
        lock_root.mkdir(parents=True, exist_ok=True)
        handle = os.open(lock_root / _LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        # An unwritable state root is not a reason to refuse to start. The
        # renames still order the work; only the mutual exclusion is lost.
        _LOG.warning("adopting without a lock (%s): %s", lock_root, exc)
        yield
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def _promote(src: Path, dst: Path) -> None:
    """Move src onto dst so no interruption can publish a partial copy.

    `shutil.move` across filesystems is copy-then-remove. A copy killed
    halfway leaves a truncated file where the real one should be, the next run
    sees a destination that exists and declines to adopt over it, and the
    operator's good copy is the one left behind. That is the silent loss this
    staging exists to make impossible.

    The copy lands on `.copying`, which is never trusted and always discarded;
    one atomic rename makes it `.adopting`, which means finished; one more
    makes it the real name. Every point a kill can land leaves exactly one of
    those three states, and each says unambiguously what to do next.
    """
    copying = dst.parent / f"{dst.name}{_COPYING_SUFFIX}"
    adopting = dst.parent / f"{dst.name}{_ADOPTING_SUFFIX}"
    _discard(copying)
    shutil.move(str(src), str(copying))
    os.replace(copying, adopting)
    os.replace(adopting, dst)


def _finish_interrupted(dst: Path) -> bool:
    """Complete a copy that finished before the last rename could run.

    `.adopting` is the only complete copy of the entry and it sits under a
    name nothing reads. Recovering it is the whole reason the staging has two
    names rather than one.
    """
    adopting = dst.parent / f"{dst.name}{_ADOPTING_SUFFIX}"
    if not adopting.exists():
        return False
    if dst.exists():
        _discard(adopting)
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(adopting, dst)
    return True


def adopt(
    legacy: Path,
    target: Path,
    lock_root: Path | None = None,
    breadcrumb: bool = True,
) -> bool:
    """Move `legacy` to `target`, once. Returns whether anything moved.

    Non-destructive in both directions. A target that already exists is the
    newer of the two and is left alone, and so is the legacy copy: an operator
    who ran a new build once and then went back has state in both places, and
    guessing which one they want is how a spend ledger or a trust file gets
    silently reset.

    `breadcrumb=False` is for the one source that is not a NxtOS root:
    `runtime_state` adopts out of `<workspace>/nxtos/`, and leaving a marker
    there would put a NxtOS file back in the operator's repo, which is the
    thing that migration exists to stop.
    """
    legacy = Path(legacy)
    target = Path(target)

    sidecars = [
        (legacy.parent / f"{legacy.name}{s}", target.parent / f"{target.name}{s}")
        for s in _SQLITE_SIDECARS
    ]
    interesting = [(legacy, target), *sidecars]
    if not any(
        src.exists() or dst.parent.joinpath(f"{dst.name}{_ADOPTING_SUFFIX}").exists()
        for src, dst in interesting
    ):
        return False

    moved = False
    with _cross_process_lock(state_home() if lock_root is None else lock_root):
        # Sidecars first, database last. At every intermediate point the
        # target either has no database at all, or has one with its WAL
        # already beside it; it never has a database whose committed pages
        # were left behind.
        for src, dst in [*sidecars, (legacy, target)]:
            if _finish_interrupted(dst):
                moved = True
                continue
            if not src.exists():
                continue
            if dst.exists():
                if src == legacy:
                    _LOG.warning(
                        "not adopting %s: %s already exists; the legacy copy is "
                        "left in place", src, dst,
                    )
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                _promote(src, dst)
            except OSError as exc:
                # A path NxtOS cannot move is not a reason to refuse to start:
                # the new location simply begins empty and the operator still
                # has their old one where they left it.
                _LOG.warning("could not adopt %s into %s: %s", src, dst, exc)
                continue
            moved = True

        if moved and breadcrumb:
            crumb = breadcrumb_path(legacy)
            try:
                crumb.parent.mkdir(parents=True, exist_ok=True)
                crumb.write_text(f"{target}\n", encoding="utf-8")
            except OSError as exc:
                _LOG.warning("could not leave a breadcrumb at %s: %s", crumb, exc)
            _LOG.info("adopted %s into %s", legacy, target)
    return moved


def _legacy_state_home(env: dict | None, home: Path | str | None) -> Path:
    """Where the host kept per-workspace state before N1.

    Resolved the pre-N1 way on purpose. An operator who set `XDG_STATE_HOME`
    or `NXTOS_STATE_HOME` has their sessions under that root and nowhere else,
    so adoption that looked only at the platform default would report a clean
    migration having moved nothing.
    """
    environ = os.environ if env is None else env
    override = (environ.get(_STATE_HOME_ALIAS) or "").strip()
    if override:
        return Path(override).expanduser()
    xdg = (environ.get("XDG_STATE_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / "nxtos"
    home_dir = _home(home)
    if DARWIN:
        return home_dir / "Library" / "Application Support" / "NxtOS"
    return home_dir / ".local" / "state" / "nxtos"


def _match(component: str, role: str, relative: str) -> tuple[Legacy, str] | None:
    """The legacy entry for one normative path, plus the workspace key if any."""
    for entry in LEGACY.get(component, ()):
        if entry.role != role:
            continue
        if entry.relative == relative:
            return entry, ""
        if KEY_TOKEN not in entry.relative:
            continue
        prefix, _, suffix = entry.relative.partition(KEY_TOKEN)
        if not relative.startswith(prefix) or not relative.endswith(suffix):
            continue
        key = relative[len(prefix):len(relative) - len(suffix) or None]
        if WORKSPACE_KEY_PATTERN.match(key):
            return entry, key
    return None


def _legacy_source(
    entry: Legacy, key: str, env: dict | None, home: Path | str | None
) -> Path:
    if KEY_TOKEN in entry.path and entry.role == "state":
        # The per-workspace root is the one legacy location an operator could
        # relocate, so it is resolved rather than spelled.
        return _legacy_state_home(env, home) / key
    return entry.legacy_path(home, DARWIN, key)


def resolve(
    component: str,
    role: str,
    relative: str = "",
    env: dict | None = None,
    home: Path | str | None = None,
    adopt_legacy: bool = True,
) -> Path:
    """The normative path, with its legacy content adopted on first resolve.

    Every call site in the shipped packages goes through here rather than
    joining a root of its own. Adoption runs at most once per (legacy, target)
    per process: `resolve` is called on every path lookup, and re-walking the
    legacy tree on each one would put a filesystem stat storm behind an
    attribute read.
    """
    if role not in ROLES:
        raise ValueError(f"{role!r} is not a role; known roles are {', '.join(ROLES)}")
    base = _component_dir(role, component, env, home)
    target = base / relative if relative else base
    if not adopt_legacy:
        return target

    match = _match(component, role, relative)
    if match is None or match[0].by_command:
        return target
    entry, key = match

    legacy = _legacy_source(entry, key, env, home)
    cache_key = f"{legacy}\0{target}"
    with _adopt_thread_lock:
        if cache_key in _adopted:
            return target
        _adopted.add(cache_key)

    adopt(legacy, target, state_home(env, home))
    return target


def breadcrumbs(home: Path | str | None = None) -> dict[Path, str]:
    """Every legacy location this machine has already migrated, and where to.

    What `nxtos doctor` reports from 0.3.0 so an operator can see what is left
    before the legacy reader goes away in 0.5.0.
    """
    found: dict[Path, str] = {}
    for component, entries in LEGACY.items():
        for entry in entries:
            legacy = entry.legacy_path(home, DARWIN)
            if KEY_TOKEN in str(legacy):
                parent = legacy.parent
                candidates = (
                    sorted(parent.glob(f"*{BREADCRUMB_SUFFIX}"))
                    if parent.is_dir() else []
                )
            else:
                candidates = [breadcrumb_path(legacy)]
            for crumb in candidates:
                try:
                    found[crumb] = crumb.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
    return found


# ---------------------------------------------------------------------------
# Which shape of NxtOS is running (N7)
# ---------------------------------------------------------------------------

#: An editable install from a checkout. Console scripts, builtin plugins and
#: the frontend all resolve to whatever branch that checkout has out.
SHAPE_DEV = "dev"
#: Wheels in a venv. Nothing resolves back into a checkout.
SHAPE_INSTALLED = "installed"
#: A packaged `.app`, which sets `NXTOS_SHAPE` in its own launcher because a
#: frozen bundle has no distribution metadata to read.
SHAPE_APP = "app"
#: `nxthooks._vendor` is not an installed distribution at all: running from a source
#: checkout on `sys.path` with no `pip install` of any kind behind it.
SHAPE_UNKNOWN = "unknown"

SHAPES = (SHAPE_DEV, SHAPE_INSTALLED, SHAPE_APP, SHAPE_UNKNOWN)

SHAPE_ENV = "NXTOS_SHAPE"

DISTRIBUTION = "nxthooks._vendor"


@dataclass(frozen=True)
class InstallShape:
    """Which shape is running, where it came from, and the sentence to print.

    One derivation with three consumers (`nxtos doctor`, the Settings About
    pane, every ledger record), because the question "which shape wrote this
    slot" is only answerable if all three spell the answer the same way.
    """

    shape: str
    origin: str = ""
    version: str = ""
    branch: str = ""

    def __str__(self) -> str:
        if self.shape == SHAPE_DEV:
            where = f"editable from {self.origin}" if self.origin else "editable"
            if self.branch:
                where += f", branch {self.branch}"
            return f"{self.shape} ({where})"
        if self.shape == SHAPE_INSTALLED:
            where = f"venv {self.origin}" if self.origin else "no prefix"
            return f"{self.shape} ({where}, {DISTRIBUTION} {self.version})"
        if self.shape == SHAPE_APP:
            return f"{self.shape} ({self.origin or 'packaged bundle'})"
        return f"{self.shape} (no {DISTRIBUTION} distribution metadata on this interpreter)"


def _direct_url(distribution: str) -> dict:
    """PEP 610 `direct_url.json`, or `{}` when the distribution has none.

    An install from an index carries no `direct_url.json` at all, which is
    itself the answer: it did not come from a directory, so it is not
    editable. Only an unreadable or malformed file is an absence of evidence,
    and that is what `{}` also means here -- the caller then falls through to
    `installed`, which is the safe direction: claiming `dev` would tell an
    operator his harnesses are wired from a checkout when they are not.
    """
    from importlib import metadata

    try:
        raw = metadata.distribution(distribution).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        raise
    except OSError:
        return {}
    if not raw:
        return {}
    import json

    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _metadata_dir(dist) -> Path | None:
    """Where this distribution's metadata actually lives, or None.

    `_path` is private and only `PathDistribution` has it; every other loader
    (a zipimport metadata finder, a frozen bundle) legitimately has no
    directory, and `None` is the honest answer for those rather than a guess.
    """
    found = getattr(dist, "_path", None)
    return Path(found) if found is not None else None


def _checkout_branch(origin: Path) -> str:
    """The branch a checkout has out, read from `.git/HEAD`, or "".

    A file read, not a subprocess: `doctor` is the thing a stranger pastes
    into an issue and must not depend on git being installed. A worktree's
    `.git` is a file pointing at the real gitdir, which is followed once.
    """
    head = origin / ".git"
    try:
        if head.is_file():
            pointer = head.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return ""
            head = Path(pointer.split(":", 1)[1].strip())
        head = head / "HEAD"
        ref = head.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return ref.split("refs/heads/", 1)[1] if "refs/heads/" in ref else ""


def _url_to_path(url: str) -> str:
    if not url.startswith("file://"):
        return url
    from urllib.parse import unquote, urlsplit

    return unquote(urlsplit(url).path)


def install_shape(env: dict | None = None,
                  distribution: str = DISTRIBUTION) -> InstallShape:
    """Dev or installed, from distribution metadata rather than a heuristic.

    PEP 610 records how a distribution was installed. `dir_info.editable` is
    true exactly when `pip install -e` put it there, so the answer is a
    metadata read with no guessing -- which matters because the alternative
    heuristics (is `__file__` under site-packages, is there a `.git` nearby)
    are both wrong for a wheel installed into a checkout's own venv.

    `NXTOS_SHAPE` wins, because a packaged `.app` freezes its metadata away
    and has to be able to say so.
    """
    environ = os.environ if env is None else env
    override = (environ.get(SHAPE_ENV) or "").strip()
    if override in SHAPES:
        return InstallShape(shape=override, origin=sys.prefix)
    if override:
        # Warned, then ignored, rather than propagated or raised. A typo in an
        # environment variable must not put a word nobody defined into every
        # ledger record, and it must not stop `doctor` answering either --
        # `doctor` is the thing a stranger pastes into an issue when something
        # is already wrong.
        _LOG.warning("%s=%r is not one of %s; reading the distribution instead",
                     SHAPE_ENV, override, ", ".join(SHAPES))

    from importlib import metadata

    try:
        version = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return InstallShape(shape=SHAPE_UNKNOWN)

    try:
        dist = metadata.distribution(distribution)
        info = _direct_url(distribution)
    except metadata.PackageNotFoundError:  # pragma: no cover - raced uninstall
        return InstallShape(shape=SHAPE_UNKNOWN)

    # A source checkout's own `nxthooks._vendor.egg-info` shadows the installed
    # distribution whenever the interpreter's cwd is the checkout, and it
    # carries no `direct_url.json` -- so without this the answer from inside
    # the tree is `installed`, which is exactly backwards and is the reading
    # an operator would act on.
    egg = _metadata_dir(dist)
    if egg is not None and egg.suffix in (".egg-info", ".egg-link"):
        origin = str(egg.parent.resolve())
        return InstallShape(shape=SHAPE_DEV, origin=origin, version=version,
                            branch=_checkout_branch(Path(origin)))

    if (info.get("dir_info") or {}).get("editable") is True:
        origin = _url_to_path(str(info.get("url") or ""))
        return InstallShape(shape=SHAPE_DEV, origin=origin, version=version,
                            branch=_checkout_branch(Path(origin)) if origin else "")
    return InstallShape(shape=SHAPE_INSTALLED, origin=sys.prefix, version=version)


# ---------------------------------------------------------------------------
# Uninstall completeness
# ---------------------------------------------------------------------------

def prune_empty_roots(env: dict | None = None,
                      home: Path | str | None = None) -> list[Path]:
    """Remove every directory NxtOS emptied, deepest first. Returns what went.

    Uninstall completeness has a half nobody writes: a component removes the
    files it wrote and leaves the shape of them behind. Four `nxt/` role
    roots, the per-component directories under them, and the XDG scaffolding
    above them are, on a machine that had none of it before, the residue an
    uninstall claims not to leave -- `.config/`, `.local/`, `.local/state/`
    and `.local/share/nxt/os/plugins/` all survived a full install-uninstall
    cycle until this function existed.

    Two bounds, and both are the safety argument:

    **Empty.** A directory with anything in it is never touched, so a
    component that removed its own file cannot take a sibling component's
    with it, and a directory the operator keeps things in is never a
    candidate. That is what makes this safe to call from any component rather
    than only from the host.

    **Under `$HOME`.** The walk upward stops at `$HOME` itself, so a relocated
    root (`NXT_STATE_HOME=/srv/nxt`) never leads to an `rmdir` of `/srv`, and
    `$HOME` is never itself a candidate.
    """
    base = _home(home)
    removed: list[Path] = []
    for root in (config_home(env, home), data_home(env, home),
                 state_home(env, home), cache_home(env, home)):
        if not root.is_dir():
            continue
        for child in sorted((p for p in root.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
            _rmdir_if_empty(child, removed)
        current = root
        while _rmdir_if_empty(current, removed):
            parent = current.parent
            if parent == current or parent == base or base not in parent.parents:
                break
            current = parent
    return removed


def _rmdir_if_empty(path: Path, removed: list[Path]) -> bool:
    try:
        if not path.is_dir() or any(path.iterdir()):
            return False
        path.rmdir()
    except OSError:
        return False
    removed.append(path)
    return True
