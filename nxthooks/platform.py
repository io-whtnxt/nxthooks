"""Where NxtHooks keeps its config and its installed runtime.

Nothing here decides anything any more. `nxthooks._paths` owns every root NxtHooks
uses, and this module is the `hooks` component's view of two of
them, kept as a name so the twenty call sites inside this package do not each
have to know their own component id.

The per-platform reasoning that used to live here (XDG on POSIX, %APPDATA% on
Windows) moved with the decision. Nothing else in this package may spell these
paths itself: every prior hard-coded ~/.config and ~/.local/share is why "does
this run on Windows" had no answer short of reading every file.
"""

from __future__ import annotations

from pathlib import Path

from . import _paths as paths

COMPONENT = "hooks"


def config_dir(env: dict | None = None, home: Path | str | None = None) -> Path:
    return paths.config_dir(COMPONENT, env, home)


def data_dir(env: dict | None = None, home: Path | str | None = None) -> Path:
    return paths.data_dir(COMPONENT, env, home)


def library_dir(env: dict | None = None, home: Path | str | None = None) -> Path:
    """Fleet-wide hooks, provided by an installer rather than an operator.

    Data rather than config: a library hook is installed software on this
    machine, not something the operator authored. Resolved per call, never
    cached at import.
    """
    return data_dir(env, home) / "library"


def state_dir(env: dict | None = None, home: Path | str | None = None) -> Path:
    return paths.state_dir(COMPONENT, env, home)


def cache_dir(env: dict | None = None, home: Path | str | None = None) -> Path:
    return paths.cache_dir(COMPONENT, env, home)


def hooks_d(env: dict | None = None, home: Path | str | None = None) -> Path:
    return config_dir(env, home) / "hooks.d"
