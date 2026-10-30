"""The roots NxtHooks and the engines it ships keep state under.

A deliberate subset of `nxtoskit.paths`, not a second opinion: the same
`$XDG_*_HOME/nxt/<component>` convention, the same `NXT_*_HOME` overrides, the
same answers (`tests/test_nxthooks_standalone.py` pins that against the full
resolver). It adopts only the legacy files of the components it ships, the
grep and lint engines, with the same move-once semantics; the rest of the
`~/.nxtos` and App Support adoption, and the roots of components NxtHooks does
not touch, are left to NxtOS.

Role roots, normative:

    role     POSIX                                 env override
    config   $XDG_CONFIG_HOME/nxt/<id>             NXT_CONFIG_HOME
    data     $XDG_DATA_HOME/nxt/<id>               NXT_DATA_HOME
    state    $XDG_STATE_HOME/nxt/<id>              NXT_STATE_HOME
    cache    $XDG_CACHE_HOME/nxt/<id>              NXT_CACHE_HOME

The `NXT_*_HOME` overrides replace the whole `nxt/` root. Windows: config under
`%APPDATA%`, the other three under `%LOCALAPPDATA%\\nxt\\<id>\\<role>`.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path

_LOG = logging.getLogger(__name__)

WINDOWS = os.name == "nt"

ROOT_NAME = "nxt"

#: `hooks` is this package. `grep` and `lint` are the engines it ships. `git`
#: is where the snapshot engine keeps its shadow store, which is NxtGit's tree
#: by long-standing agreement. `os` is the NxtOS plugin root the pane is
#: delivered into.
COMPONENT_IDS = ("os", "hooks", "git", "grep", "lint")

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

#: Named the host's state root before the roots were unified; honoured as an
#: alias for `NXT_STATE_HOME`, exactly as `nxtoskit.paths` does.
_STATE_HOME_ALIAS = "NXTOS_STATE_HOME"


class UnknownComponent(ValueError):
    """Raised for a component id outside `COMPONENT_IDS`."""


def _home(home: Path | str | None) -> Path:
    return Path.home() if home is None else Path(home)


def _root(role: str, env: dict | None, home: Path | str | None) -> Path:
    environ = os.environ if env is None else env

    override = (environ.get(f"NXT_{role.upper()}_HOME") or "").strip()
    if not override and role == "state":
        override = (environ.get(_STATE_HOME_ALIAS) or "").strip()
    if override:
        return Path(override).expanduser()

    var, parts = (_WINDOWS_BASES if WINDOWS else _POSIX_BASES)[role]
    raw = (environ.get(var) or "").strip()
    base = Path(raw).expanduser() if raw else _home(home).joinpath(*parts)
    return base / ROOT_NAME


def config_home(env: dict | None = None, home: Path | str | None = None) -> Path:
    return _root("config", env, home)


def data_home(env: dict | None = None, home: Path | str | None = None) -> Path:
    return _root("data", env, home)


def state_home(env: dict | None = None, home: Path | str | None = None) -> Path:
    return _root("state", env, home)


def cache_home(env: dict | None = None, home: Path | str | None = None) -> Path:
    return _root("cache", env, home)


def _component_dir(role: str, component: str, env: dict | None, home: Path | str | None) -> Path:
    if component not in COMPONENT_IDS:
        raise UnknownComponent(
            f"{component!r} is not a component id; known ids are {', '.join(COMPONENT_IDS)}"
        )
    root = _root(role, env, home)
    if WINDOWS and role != "config":
        return root / component / role
    return root / component


def config_dir(component: str, env: dict | None = None, home: Path | str | None = None) -> Path:
    return _component_dir("config", component, env, home)


def data_dir(component: str, env: dict | None = None, home: Path | str | None = None) -> Path:
    return _component_dir("data", component, env, home)


def state_dir(component: str, env: dict | None = None, home: Path | str | None = None) -> Path:
    return _component_dir("state", component, env, home)


def cache_dir(component: str, env: dict | None = None, home: Path | str | None = None) -> Path:
    return _component_dir("cache", component, env, home)


_APP_SUPPORT = "~/Library/Application Support/NxtOS"

#: Where the engines' files lived before the roots were unified, keyed by the
#: normative `(component, role, relative)` they map onto. The `grep` and
#: `lint` entries of `nxtoskit.paths.LEGACY`, copied rather than imported
#: (`tests/test_nxthooks_legacy_adoption.py` pins them equal): without them an
#: upgraded machine ignores the operator's grep policy and pattern pack. The
#: App Support spelling is used on every platform, as it is there.
LEGACY: dict[tuple[str, str, str], str] = {
    ("lint", "data", "bin/nxtlint-hook.py"): "~/.nxtos/bin/nxtlint-hook.py",
    ("grep", "config", "patterns.conf"): f"{_APP_SUPPORT}/nxtgrep/patterns.conf",
    ("grep", "config", "config.toml"): "~/.config/nxt/nxtgrep/config.toml",
}

#: Left beside a moved legacy file naming where it went; `nxtoskit.paths`
#: reports the same spelling from `nxtos doctor`.
BREADCRUMB_SUFFIX = ".MIGRATED-TO"

_adopted: set[tuple[Path, Path]] = set()
_adopted_lock = threading.Lock()


def _adopt(legacy: Path, target: Path) -> None:
    """Move the legacy file to `target`, once, as `nxtoskit.paths.adopt` does.

    A target that exists is the newer of the two and wins; the legacy copy is
    then left where it is. The copy is published with a hard link, which
    refuses an existing target, so a concurrent writer is never clobbered.
    """
    if not legacy.is_file():
        return
    if target.exists():
        _LOG.warning("not adopting %s: %s already exists; the legacy copy is left in place",
                     legacy, target)
        return
    staged = target.with_name(f"{target.name}.adopting-{os.getpid()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, staged)
        os.link(staged, target)
    except FileExistsError:
        return
    except OSError as exc:
        _LOG.warning("could not adopt %s into %s: %s", legacy, target, exc)
        return
    finally:
        staged.unlink(missing_ok=True)
    try:
        legacy.unlink()
        legacy.with_name(legacy.name + BREADCRUMB_SUFFIX).write_text(f"{target}\n", encoding="utf-8")
    except OSError as exc:
        _LOG.warning("adopted %s into %s but could not retire the legacy copy: %s",
                     legacy, target, exc)


def resolve(component: str, role: str, relative: str = "",
            env: dict | None = None, home: Path | str | None = None) -> Path:
    """`<role>_dir(component)/relative`, its legacy file adopted on the first
    resolve in this process."""
    if role not in ROLES:
        raise ValueError(f"{role!r} is not a role; known roles are {', '.join(ROLES)}")
    base = _component_dir(role, component, env, home)
    target = base / relative if relative else base
    spelling = LEGACY.get((component, role, relative))
    if spelling is None:
        return target
    legacy = Path(spelling.replace("~", str(_home(home)), 1))
    with _adopted_lock:
        if (legacy, target) in _adopted:
            return target
        _adopted.add((legacy, target))
    _adopt(legacy, target)
    return target


# ---------------------------------------------------------------------------
# Install shape
# ---------------------------------------------------------------------------

SHAPE_DEV = "dev"
SHAPE_INSTALLED = "installed"
SHAPE_APP = "app"
SHAPE_UNKNOWN = "unknown"
SHAPES = (SHAPE_DEV, SHAPE_INSTALLED, SHAPE_APP, SHAPE_UNKNOWN)
SHAPE_ENV = "NXTOS_SHAPE"

#: The distribution that carries this package: its own, standalone, or the
#: NxtOS one it ships inside. The first that is installed answers.
DISTRIBUTIONS = ("nxthooks", "nxtoskit")


def install_shape(env: dict | None = None) -> str:
    """`dev`, `installed`, `app` or `unknown`: the shape that wrote a ledger slot.

    PEP 610 `direct_url.json` says whether the distribution was installed
    editable, which is a metadata read rather than a guess. `NXTOS_SHAPE` wins,
    because a packaged app freezes its metadata away and has to be able to say so.
    """
    environ = os.environ if env is None else env
    override = (environ.get(SHAPE_ENV) or "").strip()
    if override in SHAPES:
        return override

    import json
    from importlib import metadata

    for name in DISTRIBUTIONS:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        # A source checkout's own `.egg-info` shadows the installed distribution
        # when the cwd is the checkout, and carries no `direct_url.json`.
        located = getattr(dist, "_path", None)
        if located is not None and Path(located).suffix in (".egg-info", ".egg-link"):
            return SHAPE_DEV
        try:
            raw = dist.read_text("direct_url.json")
            info = json.loads(raw) if raw else {}
        except (OSError, ValueError):
            info = {}
        editable = isinstance(info, dict) and (info.get("dir_info") or {}).get("editable") is True
        return SHAPE_DEV if editable else SHAPE_INSTALLED
    return SHAPE_UNKNOWN


# ---------------------------------------------------------------------------
# Uninstall completeness
# ---------------------------------------------------------------------------

def prune_empty_roots(env: dict | None = None, home: Path | str | None = None) -> list[Path]:
    """Remove every empty directory under the four `nxt/` role roots, deepest
    first, then the empty XDG scaffolding above them. Returns what went.

    Two bounds are the safety argument. A directory with anything in it is
    never touched, so a sibling component's files survive. And the walk upward
    stops at `$HOME`, so a relocated root never leads to an `rmdir` of `/srv`.
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
