"""Where hooks come from. One resolution model, three tiers."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .manifest import ManifestError, _validate, hooks_d_default
from .platform import library_dir

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"


@dataclass(frozen=True)
class Collision:
    """A hook name a tier declared but could not claim, and who kept it.

    Reported rather than dropped: a collision an operator cannot see is a
    collision that got resolved by accident, and this sits on the tool-call
    path.
    """

    name: str
    tier: str
    kept_by: str
    #: The file the losing entry came from. Without it the warning names a
    #: hook and leaves the operator to find which of several manifests
    #: declared it, which is why these sat unactioned for months.
    origin: str = ""


@dataclass(frozen=True)
class Refused:
    """A manifest one tier offered that the loader would not take -- the whole
    file, or only the entries named in `detail`.

    One record per file, because the report, the remedy and the exit code all
    speak per file: two refusals of one file read as two files (review
    e097e80b, T2337). It carries its tier and its file because the remedy
    differs by both -- an operator fixes their own `hooks.d`, `nxthooks
    install` never writes the library tier, and the builtin tier ships inside
    the package -- and `loaded` names the hooks the same file still
    contributes, because a file that still runs a guard is never a file to
    delete.
    """

    tier: str
    path: Path
    detail: str
    loaded: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolution:
    """What one pass over the three tiers learned, including what it had to
    refuse on the way."""

    entries: tuple[dict, ...] = ()
    collisions: tuple[Collision, ...] = ()
    refused: tuple[Refused, ...] = ()


def _load_dir(directory: Path, tier: str) -> tuple[list[dict], list[Refused]]:
    """One walk of one directory: the hooks it yields *and* the manifests it
    refused, returned together.

    Together because the verify report walked the same directories a second
    time to ask what had been dropped, so every refusal was said twice on
    stderr and neither copy said which tier it came from (T2337, review
    d0cf8532). Each is still said once here: stderr is the only channel the
    dispatch path has, and a dropped guard has to reach it rather than only a
    log nobody reads (T2290, review 856094e0).
    """
    entries: list[dict] = []
    refused: list[Refused] = []
    if not directory.is_dir():
        return entries, refused

    def skip(path: Path, detail: str, loaded: tuple[str, ...] = ()) -> None:
        refused.append(Refused(tier, path, detail, loaded))
        print(f"[nxthooks] skipping {tier} manifest: {detail}", file=sys.stderr, flush=True)

    for path in sorted(directory.glob("*.toml")):
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            skip(path, f"{path.name}: {exc}")
            continue
        declared = data.get("hook", [])
        if not isinstance(declared, list):
            # `[hook]` iterates as its keys and `hook = "x"` as its characters,
            # so either reached `_validate` as a string and raised
            # AttributeError: one mistyped manifest took the whole report down
            # instead of appearing in it as the manifest it is.
            skip(path, f"{path.name}: 'hook' must be an array of [[hook]] "
                       f"tables, found {type(declared).__name__}")
            continue
        loaded: list[str] = []
        rejected: list[str] = []
        for position, raw in enumerate(declared, start=1):
            if not isinstance(raw, dict):
                rejected.append(f"{path.name}: [[hook]] entry {position} is "
                                "not a table")
                continue
            try:
                entry = _validate(raw, path.name)
            except ManifestError as exc:
                rejected.append(str(exc))
                continue
            loaded.append(entry["name"])
            entries.append({**entry, "tier": tier, "origin": str(path)})
        if rejected:
            # Said once for the file, with every entry it refused: the operator
            # fixes one file, and the report counts one gap.
            skip(path, rejected[0] if len(rejected) == 1 else
                 f"{path.name}: {len(rejected)} of {len(declared)} hooks refused "
                 f"-- {'; '.join(rejected)}",
                 tuple(loaded))
    return entries, refused


def builtin_manifests() -> list[dict]:
    return _load_dir(BUILTIN_DIR, "builtin")[0]


def load_tiers(
    library: Path | None = None,
    hooks_d: Path | None = None,
    *,
    env: dict | None = None,
    home: Path | str | None = None,
) -> Resolution:
    """The whole registry in one pass: builtin, library, user, and the
    manifests each of the three had to refuse.

    `resolve` is the projection for a caller that only wants the hooks to run;
    the report takes everything from here, which is why the refusal of a
    builtin or library manifest reaches it at all.
    """
    root = library if library is not None else library_dir(env, home)
    user_dir = hooks_d if hooks_d is not None else hooks_d_default(env, home)
    builtin, builtin_refused = _load_dir(BUILTIN_DIR, "builtin")
    library_entries, library_refused = _load_dir(root / "hooks.d", "library")
    user_entries, user_refused = _load_dir(user_dir, "user")

    resolved: dict[str, dict] = {e["name"]: e for e in builtin}
    collisions: list[Collision] = []

    library_names = {e["name"] for e in library_entries}
    for entry in library_entries:
        resolved[entry["name"]] = entry

    for entry in user_entries:
        if entry["name"] in library_names:
            collisions.append(
                Collision(name=entry["name"], tier="user",
                          kept_by="library", origin=entry.get("origin", ""))
            )
            continue
        resolved[entry["name"]] = entry

    return Resolution(
        entries=tuple(resolved[k] for k in sorted(resolved)),
        collisions=tuple(collisions),
        refused=(*builtin_refused, *library_refused, *user_refused),
    )


def resolve(
    library: Path | None = None,
    hooks_d: Path | None = None,
    *,
    env: dict | None = None,
    home: Path | str | None = None,
) -> tuple[list[dict], tuple[Collision, ...]]:
    """The hooks to run, and the collisions between tiers.

    The refused manifests are not part of this view -- `load_tiers` returns
    them, and `_load_dir` says each one on stderr as it is dropped.
    """
    resolution = load_tiers(library, hooks_d, env=env, home=home)
    return list(resolution.entries), resolution.collisions


def render_collisions(collisions: tuple[Collision, ...]) -> None:
    for c in collisions:
        where = f" -- {c.origin}" if c.origin else ""
        print(f"  [warn] hook '{c.name}' from {c.tier} lost to {c.kept_by}"
              f"; delete or rename it to clear this{where}")
