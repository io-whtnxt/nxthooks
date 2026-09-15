"""Where hooks come from. One resolution model, three tiers."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .manifest import ManifestError, _validate, hooks_d_default
from .platform import library_dir

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"


def _load_dir(directory: Path, tier: str) -> list[dict]:
    entries: list[dict] = []
    if not directory.is_dir():
        return entries
    for path in sorted(directory.glob("*.toml")):
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            print(f"[nxthooks] skipping {path.name}: {exc}", flush=True)
            continue
        for raw in data.get("hook", []):
            try:
                entry = _validate(raw, path.name)
            except ManifestError as exc:
                print(f"[nxthooks] skipping hook: {exc}", flush=True)
                continue
            entries.append({**entry, "tier": tier, "origin": str(path)})
    return entries


def builtin_manifests() -> list[dict]:
    return _load_dir(BUILTIN_DIR, "builtin")


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


def resolve(
    library: Path | None = None,
    hooks_d: Path | None = None,
) -> tuple[list[dict], tuple[Collision, ...]]:
    root = library if library is not None else library_dir()
    resolved: dict[str, dict] = {e["name"]: e for e in builtin_manifests()}
    collisions: list[Collision] = []

    library_names: set[str] = set()
    for entry in _load_dir(root / "hooks.d", "library"):
        resolved[entry["name"]] = entry
        library_names.add(entry["name"])

    user_dir = hooks_d if hooks_d is not None else hooks_d_default()
    for entry in _load_dir(user_dir, "user"):
        if entry["name"] in library_names:
            collisions.append(
                Collision(name=entry["name"], tier="user",
                          kept_by="library", origin=entry.get("origin", ""))
            )
            continue
        resolved[entry["name"]] = entry

    return [resolved[k] for k in sorted(resolved)], tuple(collisions)


def render_collisions(collisions: tuple[Collision, ...]) -> None:
    for c in collisions:
        where = f" -- {c.origin}" if c.origin else ""
        print(f"  [warn] hook '{c.name}' from {c.tier} lost to {c.kept_by}"
              f"; delete or rename it to clear this{where}")
