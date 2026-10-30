"""NxtGrep's engine: pack loading and payload scanning, as pure functions.

No I/O decisions live here beyond reading the pack files it is handed — config
resolution belongs to config.py, verdict formatting to whichever surface is
speaking (hook wrapper, harness adapter). Keeping this importable without any
harness or runtime machinery is what lets every adapter share one scanner.
"""
import re
from dataclasses import dataclass
from pathlib import Path

# A tool call carrying more than max_bytes of string data cannot be fully
# scanned inside the manifest timeout, and partially-scanned is not scanned.
DEFAULT_MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class Pack:
    """One loaded regex file. `source` names the file so a refusal can say
    which pack spoke; each rule keeps its source line as its label, because a
    label maintained separately from its regex is a label that drifts."""

    source: str
    rules: tuple[tuple[str, re.Pattern], ...]


@dataclass(frozen=True)
class Hit:
    rule_source: str
    location: str  # JSON-ish path into the payload, e.g. tool_input.content
    snippet: str


def load_pack(path: Path) -> tuple[Pack | None, str | None]:
    """Read one pack file. Returns (pack, None) or (None, error-reason).

    A missing file is not an error: packs are opt-in by existence, which is
    what lets the user pack be absent on a fresh install. A malformed line IS
    an error — fail-closed, naming file and line.
    """
    if not path.is_file():
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{path}: cannot read pack: {exc}"

    rules: list[tuple[str, re.Pattern]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rules.append((line, re.compile(line, re.IGNORECASE)))
        except re.error as exc:
            return None, f"{path}:{lineno}: bad regex {line!r}: {exc}"
    return Pack(source=str(path), rules=tuple(rules)), None


def iter_strings(value, path: str = "tool_input"):
    """Yield (location, string) for every string reachable in the payload.

    Locations are JSON-ish paths (`tool_input.content`), which is what makes a
    refusal name the exact field that carried the hit.
    """
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_strings(item, f"{path}[{index}]")


def scan(
    tool_input,
    packs: list[Pack],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_reported_hits: int = 5,
) -> tuple[list[Hit], bool]:
    """Scan every string reachable in tool_input against every pack.

    Returns (hits, truncated). Hits are capped at max_reported_hits; the
    caller decides how truncation reads in a verdict. Scanning stops at the
    first cap that trips — bytes or hits — because past either point the only
    question left is what the refusal says.
    """
    rules = [(pack.source, src, rx) for pack in packs for src, rx in pack.rules]
    hits: list[Hit] = []
    scanned_bytes = 0
    truncated = False

    for location, text in iter_strings(tool_input):
        scanned_bytes += len(text.encode("utf-8", errors="replace"))
        if scanned_bytes > max_bytes:
            truncated = True
            break
        for pack_source, source, regex in rules:
            match = regex.search(text)
            if match:
                start = max(match.start() - 20, 0)
                snippet = text[start : match.end() + 20]
                hits.append(
                    Hit(rule_source=f"[{source}]", location=location, snippet=snippet)
                )
                if len(hits) >= max_reported_hits:
                    return hits, True
    return hits, truncated
