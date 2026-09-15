"""The argv placeholder grammar, in one place because two modules read it.

`adapters/cli.py` renders a template and `manifest.py` refuses a template that
disagrees with the arguments beside it. If each parsed `{name}` and `{name?}`
for itself, the renderer and the validator could drift, and the drift would be
invisible: a manifest that validated under one grammar and rendered under
another produces an argv nobody declared.

Nothing here imports anything, deliberately. `manifest.py` runs on every UI
render, and reaching the renderer's module for a string split would drag the
desktop engine into manifest loading.
"""

from __future__ import annotations

OPTIONAL_MARK = "?"


def placeholder_name(part: str) -> tuple[str, bool] | None:
    """`(name, optional)` for one template element, or None if it is a literal."""
    if not (part.startswith("{") and part.endswith("}")):
        return None
    key = part[1:-1]
    if key.endswith(OPTIONAL_MARK):
        return key[: -len(OPTIONAL_MARK)], True
    return key, False


def placeholder_names(argv: list[str]) -> dict[str, bool]:
    """Every argument the template names, mapped to whether it may be omitted.

    A name appearing twice keeps the stricter reading: one required use is
    enough to make the argument required, since that use has no way to be
    left out.
    """
    found: dict[str, bool] = {}
    for part in argv:
        parsed = placeholder_name(part)
        if parsed is None:
            continue
        name, optional = parsed
        found[name] = optional and found.get(name, True)
    return found
