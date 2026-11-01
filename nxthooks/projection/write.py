"""Writing into a file somebody else owns, without giving it back changed.

Four properties, all load-bearing:

**Detect-gated.** Every entry point takes a `Detection` and refuses without a
positive one for the file it is about to touch. "Detect before any write" is
not a rule an author remembers; it is an argument the function will not run
without. `NotDetected` is raised for a missing, negative, or wrong-harness
detection, and for a path outside the root that detection actually looked at.

**Locked.** `flock` on `<file>.nxt.lock`, the idiom NxtSkills' `scope.py` and
`nxtmcp/hermes.py` already use, opened `O_NOFOLLOW` so a symlink planted at a
predictable name is refused rather than followed and truncated at our own
privilege.

**Compare-and-swap.** The lock only serialises Nxt writers. The harness does
not take it. So the bytes are re-read immediately before the replace, and a
change since the read means a foreign writer landed mid-edit: re-read once and
redo the mutation, and on a second collision fail with the file named rather
than clobber. The mutation callback runs between the two reads, which is also
how a test provokes the race without a hook parameter existing for it.

**Style-preserving.** The file belongs to someone else; handing it back
reformatted is a bug even when the content is right. Indent is detected from
the first nested line, and `ensure_ascii` is set from whether the file was
pure ASCII to begin with. That second one is not fussiness: Claude Code's own
writer emits UTF-8, `json.dumps` defaults to `ensure_ascii=True`, and NxtHooks
today calls `json.dumps(data, indent=2)` -- so on any settings file containing
an accented character, a wire rewrites a stranger's text into `\\uXXXX`
escapes and every subsequent foreign-change check sees a file that moved.

**On the foreign digest.** It is taken over a *canonical* serialisation of the
remainder (sorted keys, no whitespace), not over raw bytes. Claude Code was
observed reordering top-level keys when it rewrote a settings file: a raw-byte
remainder hash would flip on that, so `foreign_changed` would be true after
almost every harness save and would stop meaning anything. The plan's ledger
field keeps its name; what goes in it is the semantic hash, and a pure
reformat no longer cries wolf.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .detect import Detection
from .envelope import digest

_INDENT_RE = re.compile(r"\n(?P<indent>[ \t]+)\S")


class NotDetected(RuntimeError):
    """A write was attempted without a positive detection for this file."""


class ConcurrentModification(RuntimeError):
    """A foreign writer changed the file twice while we were editing it."""


@dataclass(frozen=True)
class JsonStyle:
    indent: int | str = 2
    ensure_ascii: bool = False
    trailing_newline: bool = True
    #: Containers the file wrote on one line, by path: the verbatim text and
    #: its parsed value. `json.dumps(indent=)` has no such form, so without
    #: this every compact nested object comes back expanded.
    inline: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class WriteOutcome:
    path: Path
    changed: bool
    foreign_before: str
    foreign_after: str
    #: How many times a foreign writer landed between the read and the write.
    retries: int = 0


def detect_json_style(text: str) -> JsonStyle:
    """What this file already looks like.

    An empty or absent file gets the shape Claude Code's own writer emits,
    because that is the one measured default in the family rather than a
    preference.
    """
    if not text.strip():
        return JsonStyle()
    match = _INDENT_RE.search(text)
    indent: int | str = 2
    if match:
        raw = match.group("indent")
        indent = raw if raw.startswith("\t") else len(raw)
    return JsonStyle(
        indent=indent,
        ensure_ascii=text.isascii(),
        trailing_newline=text.endswith("\n"),
        inline=_inline_containers(text),
    )


def _inline_containers(text: str) -> dict:
    """Path -> (verbatim text, value) for every non-empty container written on
    one line. Empty ones are skipped: `json.dumps` already writes `{}`/`[]`.
    A document that does not parse yields nothing; the caller refuses it."""
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return {}
    found: dict = {}
    # Frames: [kind, path, start, multiline, next_index, pending_key, expect_key]
    stack: list[list] = []
    i, n = 0, len(text)
    decoder = json.JSONDecoder()

    def value_path() -> tuple:
        if not stack:
            return ()
        top = stack[-1]
        return top[1] + ((top[5],) if top[0] == "{" else (top[4],))

    while i < n:
        ch = text[i]
        if ch == '"':
            value, end = decoder.raw_decode(text, i)
            if stack and stack[-1][0] == "{" and stack[-1][6]:
                stack[-1][5], stack[-1][6] = value, False
            i = end
            continue
        if ch in "{[":
            stack.append([ch, value_path(), i, False, 0, None, ch == "{"])
        elif ch in "}]":
            kind, path, start, multiline, count, _, _ = stack.pop()
            body = text[start:i + 1]
            if not multiline and body[1:-1].strip():
                found[path] = (body, json.loads(body))
        elif ch == ",":
            if stack:
                _advance(stack[-1])
        elif ch == "\n":
            for frame in stack:
                frame[3] = True
        elif not ch.isspace() and ch not in ":":
            # A bare scalar: number, true, false, null.
            _, end = decoder.raw_decode(text, i)
            i = end
            continue
        i += 1
    return found


def _advance(frame: list) -> None:
    if frame[0] == "{":
        frame[6] = True
    else:
        frame[4] += 1


def dump_json(doc, style: JsonStyle) -> str:
    return _dump(doc, style, (), 0) + ("\n" if style.trailing_newline else "")


def _dump(value, style: JsonStyle, path: tuple, depth: int) -> str:
    """`json.dumps(indent=)`, except a container the file wrote on one line
    comes back on one line: verbatim while its value is unchanged."""
    ascii_only = style.ensure_ascii
    if isinstance(value, (dict, list)) and value:
        kept = style.inline.get(path)
        if kept is not None:
            return kept[0] if kept[1] == value else json.dumps(
                value, ensure_ascii=ascii_only)
        unit = style.indent if isinstance(style.indent, str) else " " * style.indent
        inner, outer = "\n" + unit * (depth + 1), "\n" + unit * depth
        if isinstance(value, dict):
            items = [f"{json.dumps(k, ensure_ascii=ascii_only)}: "
                     f"{_dump(v, style, path + (k,), depth + 1)}"
                     for k, v in value.items()]
            return "{" + inner + ("," + inner).join(items) + outer + "}"
        items = [_dump(v, style, path + (i,), depth + 1)
                 for i, v in enumerate(value)]
        return "[" + inner + ("," + inner).join(items) + outer + "]"
    return json.dumps(value, ensure_ascii=ascii_only)


def canonical_json(doc) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def foreign_digest(content, *, json_like: bool) -> str:
    """The hash of what is not ours.

    `content` is a parsed document for a JSON-like file and text otherwise.
    The caller has already removed its own slots; this only decides how to
    compare what is left.
    """
    return digest(canonical_json(content) if json_like else content)


@contextmanager
def locked(path: Path):
    """Advisory exclusive lock on `<file>.nxt.lock`."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(target.name + ".nxt.lock")
    try:
        handle = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ValueError(f"lock at {lock} is unusable: {exc.strerror}") from exc
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield lock
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def _require_detection(detection: Detection | None, path: Path) -> None:
    if detection is None:
        raise NotDetected(
            f"refusing to write {path}: no detection was performed. "
            "Never write into a file you have not just read and understood."
        )
    if not detection.present:
        raise NotDetected(
            f"refusing to write {path}: {detection.harness} is not present "
            f"({detection.reason})"
        )
    if not detection.owns(path):
        raise NotDetected(
            f"refusing to write {path}: the detection for {detection.harness} "
            f"covers {detection.root}, which does not contain it"
        )


def _atomic_replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".nxt-tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _read(path: Path) -> tuple[str, bool]:
    try:
        return path.read_text(encoding="utf-8"), True
    except FileNotFoundError:
        return "", False


def edit_text(
    path: Path | str,
    *,
    detection: Detection | None,
    mutate: Callable[[str], str],
    strip: Callable[[str], str] | None = None,
) -> WriteOutcome:
    """Read, mutate, write back under lock and compare-and-swap.

    `strip` returns the text with this component's slots removed, which is
    what the foreign digest is taken over.
    """
    target = Path(path).expanduser()
    _require_detection(detection, target)
    remainder = strip or (lambda text: text)

    with locked(target):
        for attempt in range(2):
            before, _ = _read(target)
            after = mutate(before)
            current, _ = _read(target)
            if current != before:
                continue
            changed = after != before
            if changed:
                _atomic_replace(target, after)
            return WriteOutcome(
                path=target,
                changed=changed,
                foreign_before=foreign_digest(remainder(before), json_like=False),
                foreign_after=foreign_digest(remainder(after), json_like=False),
                retries=attempt,
            )
    raise ConcurrentModification(
        f"{target} changed twice while NxtOS was editing it; nothing written"
    )


def edit_json(
    path: Path | str,
    *,
    detection: Detection | None,
    mutate: Callable[[dict], dict],
    strip: Callable[[dict], dict] | None = None,
) -> WriteOutcome:
    """The same protocol for a JSON document, preserving its detected style.

    A file that exists but does not parse is refused rather than replaced: an
    unparsable settings file is somebody's broken edit, and overwriting it
    turns a mistake they can fix into data they cannot recover.
    """
    target = Path(path).expanduser()
    _require_detection(detection, target)
    remainder = strip or (lambda doc: doc)

    with locked(target):
        for attempt in range(2):
            before_text, _ = _read(target)
            style = detect_json_style(before_text)
            if before_text.strip():
                try:
                    doc = json.loads(before_text)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{target} is not valid JSON ({exc}); refusing to "
                        "overwrite a file NxtOS did not break"
                    ) from exc
            else:
                doc = {}
            foreign_before = foreign_digest(remainder(doc), json_like=True)

            after_doc = mutate(doc)
            after_text = dump_json(after_doc, style)

            current_text, _ = _read(target)
            if current_text != before_text:
                continue
            changed = after_text != before_text
            if changed:
                _atomic_replace(target, after_text)
            return WriteOutcome(
                path=target,
                changed=changed,
                foreign_before=foreign_before,
                foreign_after=foreign_digest(remainder(after_doc), json_like=True),
                retries=attempt,
            )
    raise ConcurrentModification(
        f"{target} changed twice while NxtOS was editing it; nothing written"
    )
