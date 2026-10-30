"""The contract NxtHooks offers to whatever hosts it.

NxtOS does not pin an exact NxtHooks, it accepts a range of this number: the
major changes only when a host that worked would stop working (a verb removed, a
flag renamed, the shape of what `--contract` prints), the minor when something is
added. NxtHooks releases on its own clock inside that range.

The number lives in `component.toml` (`contractVersion`), the one file the host
already reads about every component, and `nxthooks --contract` prints it with
enough alongside to tell a host what it is talking to.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from . import CANONICAL_EVENTS, INSTALLABLE_HARNESSES, __version__


def contract_version() -> str:
    with open(Path(__file__).resolve().parent / "component.toml", "rb") as fh:
        return tomllib.load(fh)["contractVersion"]


def describe() -> dict:
    return {
        "name": "nxthooks",
        "version": __version__,
        "contract": contract_version(),
        "harnesses": list(INSTALLABLE_HARNESSES),
        "events": list(CANONICAL_EVENTS),
    }


def version_key(text: str) -> tuple[int, ...]:
    """`"0.10.2"` -> `(0, 10, 2)`. A suffix such as `rc1` ends the number, so a
    pre-release compares equal to its release; good enough to answer "is the
    installed copy newer", which is the only question asked."""
    parts: list[int] = []
    for piece in text.strip().split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
        if digits != piece:
            break
    return tuple(parts)
