"""Strip a projection whose component is already gone, by grammar alone.

This module imports no component and needs no ledger. That is the entire
point: the case that matters is the one where `pip uninstall nxthooks._vendor` ran
first, so `nxthooks.lifecycle` cannot be imported to clean up after itself,
and seven stanzas in Claude Code plus six in Codex keep pointing at a
dispatcher that is about to stop existing. On pi and OpenCode the same
situation is worse than an error, because those adapters resolve a missing
runner to `null` and the harness treats that as allow: a security guard that
degrades to nothing, with no signal.

An *orphan* is a marker whose runtime path does not exist. The runtime is read
out of the stanza itself wherever the form permits -- for a command string it
is the script the command runs -- so the sweep works on a machine with no
NxtOS state at all.

Legacy `NXTHOOKS_MANAGED=1` stanzas are swept too. They carry no component,
version or hash, so they can be recognised and removed but never verified,
which is the argument for the marker grammar stated as a code path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .detect import HARNESSES, detect
from .envelope import (
    FORM_COMMENT_LINE,
    FORM_ENV_PREFIX,
    FORM_MARKER_FILE,
    MARKER_FILE_NAME,
    MARKER_RE,
    Marker,
    MarkerError,
    command_payload,
    digest,
    is_legacy_command,
    parse_command_marker,
    parse_marker_file,
)

#: The first absolute path in a command, quoted or bare. `sh "<root>/bin/
#: cc-dispatch.sh" before_tool` is the shape both dispatchers use.
_RUNTIME_RE = re.compile(
    r"""['"](?P<quoted>/[^'"]+)['"]|(?<![\w/'"])(?P<bare>/[^\s'"]+)"""
)


@dataclass(frozen=True)
class Finding:
    """One marker found in a harness file, with everything a sweep needs."""

    harness: str
    file: Path
    slot: str
    form: str
    marker: Marker | None
    own_sha256: str
    runtime: str

    @property
    def legacy(self) -> bool:
        return self.marker is None

    @property
    def orphaned(self) -> bool:
        return bool(self.runtime) and not Path(self.runtime).exists()


@dataclass(frozen=True)
class Orphan:
    finding: Finding
    removed: bool
    detail: str


def runtime_of(command: str) -> str:
    match = _RUNTIME_RE.search(command)
    if not match:
        return ""
    return match.group("quoted") or match.group("bare") or ""


def walk_commands(doc, pointer: str = ""):
    """Every `(json-pointer, command)` in a hook document.

    Pointers are RFC 6901, which is what the ledger records as `slot` for the
    JSON forms.
    """
    if isinstance(doc, dict):
        command = doc.get("command")
        if isinstance(command, str):
            yield pointer, command
        for key, value in doc.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            yield from walk_commands(value, f"{pointer}/{token}")
    elif isinstance(doc, list):
        for index, value in enumerate(doc):
            yield from walk_commands(value, f"{pointer}/{index}")


def pointer_parent(pointer: str) -> tuple[str, str]:
    head, _, last = pointer.rpartition("/")
    return head, last.replace("~1", "/").replace("~0", "~")


def resolve_pointer(doc, pointer: str):
    """The value at an RFC 6901 pointer, or None when the path does not exist."""
    current = doc
    for token in [t for t in pointer.split("/") if t != ""]:
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if key not in current:
                return None
            current = current[key]
        elif isinstance(current, list):
            try:
                current = current[int(key)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _contains_nothing(node) -> bool:
    """Is this container empty of containers?

    An empty list or dict, and also a dict left holding only scalars after its
    one list was removed -- `{"matcher": "Bash"}` with no `hooks` is a group
    that groups nothing, and leaving it behind is residue NxtOS put in a file
    it does not own. The rule is structural, not a hook-format special case.
    """
    if isinstance(node, list):
        return not node
    if isinstance(node, dict):
        return not any(isinstance(v, (list, dict)) for v in node.values())
    return False


def prune(doc, pointer: str) -> None:
    """Remove the value at `pointer`, then every container it emptied.

    The document root is only ever popped from, never popped: the loop ends at
    the empty pointer, so a settings file that had one hook and one `model`
    key comes back holding `model`, not `{}`.
    """
    while pointer:
        parent_pointer, key = pointer_parent(pointer)
        parent = resolve_pointer(doc, parent_pointer)
        if isinstance(parent, list):
            try:
                parent.pop(int(key))
            except (ValueError, IndexError):
                return
        elif isinstance(parent, dict):
            if key not in parent:
                return
            parent.pop(key)
        else:
            return
        if not _contains_nothing(parent):
            return
        pointer = parent_pointer


def _scan_json(harness_id: str, path: Path) -> list[Finding]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    findings: list[Finding] = []
    for pointer, command in walk_commands(doc):
        marker = parse_command_marker(command)
        if marker is None and not is_legacy_command(command):
            continue
        payload = command_payload(command)
        findings.append(Finding(
            harness=harness_id, file=path, slot=pointer, form=FORM_ENV_PREFIX,
            marker=marker, own_sha256=digest(payload),
            runtime=runtime_of(payload),
        ))
    return findings


def _scan_marker_file(harness_id: str, path: Path) -> list[Finding]:
    try:
        marker, source, runtime = parse_marker_file(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, MarkerError):
        return []
    owned = path.parent
    return [Finding(
        harness=harness_id, file=path, slot=str(owned), form=FORM_MARKER_FILE,
        marker=marker, own_sha256=source, runtime=runtime or str(owned),
    )]


def _scan_comment_line(harness_id: str, path: Path) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    # Deliberately not `envelope.parse_comment_line`: that needs the suffix
    # table, and a sweep that runs with nothing installed must not refuse a
    # file whose extension it has never heard of.
    head, _, rest = text.partition("\n")
    match = MARKER_RE.fullmatch(head.strip().lstrip("/#").strip())
    if not match:
        return []
    runtime = runtime_of(rest)
    return [Finding(
        harness=harness_id, file=path, slot=str(path), form=FORM_COMMENT_LINE,
        marker=Marker(**match.groupdict()), own_sha256=digest(rest),
        runtime=runtime or str(path),
    )]


def scan(*, home: Path | str | None = None,
         env: dict | None = None) -> tuple[Finding, ...]:
    """Every Nxt marker in every file the harness table knows about."""
    root = Path.home() if home is None else Path(home)
    findings: list[Finding] = []
    for harness_id, spec in HARNESSES.items():
        base = spec.root_path(root, env)
        for pattern in spec.marker_globs:
            relative = (
                str(Path(pattern).relative_to(spec.root))
                if pattern.startswith(spec.root) else pattern
            )
            for path in sorted(base.glob(relative)):
                if not path.is_file():
                    continue
                if path.name == MARKER_FILE_NAME:
                    findings.extend(_scan_marker_file(harness_id, path))
                elif path.suffix == ".json":
                    findings.extend(_scan_json(harness_id, path))
                else:
                    findings.extend(_scan_comment_line(harness_id, path))
    return tuple(findings)


def find_orphans(*, home: Path | str | None = None,
                 env: dict | None = None) -> tuple[Finding, ...]:
    return tuple(f for f in scan(home=home, env=env) if f.orphaned)


def _remove_tree(root: Path) -> None:
    for child in sorted(root.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    root.rmdir()


def strip_orphans(*, home: Path | str | None = None, env: dict | None = None,
                  path_lookup=None) -> tuple[Orphan, ...]:
    """Remove every projection whose runtime is gone. Imports no component."""
    from .write import edit_json  # local: write imports detect, not orphans

    results: list[Orphan] = []
    by_file: dict[Path, list[Finding]] = {}
    for finding in find_orphans(home=home, env=env):
        by_file.setdefault(finding.file, []).append(finding)

    for file, findings in sorted(by_file.items()):
        detection = detect(
            findings[0].harness, home=home, env=env, path_lookup=path_lookup
        )
        if findings[0].form == FORM_ENV_PREFIX:
            pointers = sorted((f.slot for f in findings), reverse=True)

            def mutate(doc, pointers=pointers):
                for pointer in pointers:
                    prune(doc, pointer)
                return doc

            edit_json(file, detection=detection, mutate=mutate)
            results.extend(
                Orphan(f, True, f"stanza at {f.slot} removed from {file}")
                for f in findings
            )
            continue

        for finding in findings:
            owned = Path(finding.slot)
            if finding.form == FORM_MARKER_FILE and owned.is_dir():
                _remove_tree(owned)
                results.append(
                    Orphan(finding, True, f"owned directory {owned} removed")
                )
            elif finding.form == FORM_COMMENT_LINE and file.is_file():
                file.unlink()
                results.append(Orphan(finding, True, f"owned file {file} removed"))
            else:
                results.append(Orphan(finding, False, f"nothing to remove at {owned}"))
    return tuple(results)
