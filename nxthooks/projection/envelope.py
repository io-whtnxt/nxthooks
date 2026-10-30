"""The marker grammar, and the four in-file forms that carry it.

    nxt:<component>/<version>/<sha12>

`sha12` is the first twelve hex characters of the SHA-256 of the *payload*:
the stanza with the marker removed. That exclusion is not a detail. The marker
lives inside the thing it describes -- a command string carries its own
`NXT=` prefix -- so a digest over the whole stanza would have to contain
itself. Two implementations that disagree about this produce a `verify` that
reports `altered` for every slot forever, which reads exactly like drift and
is not. `command_payload` is the single answer, and
`test_the_digest_excludes_the_marker_it_is_embedded_in` pins it.

The four forms, and why each one:

`env-prefix` -- a leading shell environment assignment on a command string.
The only inert slot both Claude Code and Codex leave alone: verified on this
machine, where Claude Code rewrote a settings file through its own typed model
and preserved seven `NXTHOOKS_MANAGED=1 ...` command strings verbatim while
*deleting* an unknown `_nxt` sibling key next to one of them. A JSON object
key is not a slot in a harness that models its own schema; the inside of a
string the harness must not interpret is.

`marker-file` -- `.nxt-managed` in a directory NxtOS owns outright, the
generalisation of NxtSkills' `.nxtskills-managed`.

`comment-line` -- the first line of a single file NxtOS owns outright.

`ledger-only` -- no in-file evidence at all, for a slot whose format has no
inert slot we have *verified*. `detect.py` records what was tried and what
each harness did with it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: Command strings inside JSON hook entries (Claude Code, Codex).
FORM_ENV_PREFIX = "env-prefix"
#: `.nxt-managed` inside a directory NxtOS owns outright.
FORM_MARKER_FILE = "marker-file"
#: First line of a single file NxtOS owns outright.
FORM_COMMENT_LINE = "comment-line"
#: No in-file evidence; the ledger slot is the whole record.
FORM_LEDGER_ONLY = "ledger-only"

FORMS = (FORM_ENV_PREFIX, FORM_MARKER_FILE, FORM_COMMENT_LINE, FORM_LEDGER_ONLY)

#: The environment variable the env-prefix form assigns. The value is the
#: marker; the `nxt:` scheme prefix is optional there because the variable
#: name already carries it, and both spellings parse.
ENV_VAR = "NXT"

MARKER_FILE_NAME = ".nxt-managed"

#: NxtHooks 0.1/0.2 wrote this instead of a marker. It names no component, no
#: version and no hash, so it can only ever be recognised, never verified --
#: which is the whole argument for replacing it. Recognised through 0.4 so
#: that `unwire` and `orphans` can still clean up after the old writer.
LEGACY_ENV_PREFIX = "NXTHOOKS_MANAGED=1"

_COMPONENT = r"[a-z][a-z0-9-]*"
_VERSION = r"[0-9A-Za-z][0-9A-Za-z.+-]*"
_SHA12 = r"[0-9a-f]{12}"

MARKER_RE = re.compile(
    rf"(?:nxt:)?(?P<component>{_COMPONENT})/(?P<version>{_VERSION})/(?P<sha12>{_SHA12})"
)
_ENV_ASSIGNMENT_RE = re.compile(rf"^{ENV_VAR}=(?P<marker>\S+)\s+")
_LEGACY_ASSIGNMENT_RE = re.compile(rf"^{re.escape(LEGACY_ENV_PREFIX)}\s+")

#: How a comment starts, by file suffix. A first-line marker in the wrong
#: comment syntax is a syntax error in the file we just claimed to own.
_COMMENT_PREFIX = {
    ".ts": "//", ".tsx": "//", ".js": "//", ".mjs": "//", ".cjs": "//",
    ".py": "#", ".sh": "#", ".bash": "#", ".zsh": "#",
    ".yaml": "#", ".yml": "#", ".toml": "#", ".conf": "#", ".ini": "#",
}


class MarkerError(ValueError):
    """A marker that does not parse, or a form that cannot carry one."""


@dataclass(frozen=True)
class Marker:
    component: str
    version: str
    sha12: str

    def __str__(self) -> str:
        return f"nxt:{self.component}/{self.version}/{self.sha12}"

    @property
    def env_assignment(self) -> str:
        return f"{ENV_VAR}={self.component}/{self.version}/{self.sha12}"

    def covers(self, payload: str) -> bool:
        return self.sha12 == digest(payload)[:12]


def digest(payload: str) -> str:
    """SHA-256 of a payload, as hex. The ledger stores all of it; the marker
    stores the first twelve characters of it."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build(component: str, version: str, payload: str) -> Marker:
    if not re.fullmatch(_COMPONENT, component):
        raise MarkerError(f"{component!r} is not a component id")
    if not re.fullmatch(_VERSION, version):
        raise MarkerError(f"{version!r} is not a version")
    return Marker(component, version, digest(payload)[:12])


def parse_marker(text: str) -> Marker:
    match = MARKER_RE.fullmatch(text.strip())
    if not match:
        raise MarkerError(f"not a marker: {text!r}")
    return Marker(**match.groupdict())


# --------------------------------------------------------------- env-prefix


def command_payload(command: str) -> str:
    """The command with any Nxt marker assignment removed.

    This is what gets hashed, and it is why re-stamping a command is
    idempotent rather than accumulating prefixes.
    """
    stripped = _ENV_ASSIGNMENT_RE.sub("", command, count=1)
    return _LEGACY_ASSIGNMENT_RE.sub("", stripped, count=1)


def stamp_command(command: str, component: str, version: str) -> str:
    payload = command_payload(command)
    return f"{build(component, version, payload).env_assignment} {payload}"


def parse_command_marker(command: str) -> Marker | None:
    """The marker on a command string, or None.

    A legacy `NXTHOOKS_MANAGED=1` command answers None: it is recognisably
    ours (see `is_legacy_command`) but it carries nothing to verify against,
    and returning a fabricated marker for it would make `verify` claim a
    guarantee the old grammar never gave.
    """
    match = _ENV_ASSIGNMENT_RE.match(command)
    if not match:
        return None
    try:
        return parse_marker(match.group("marker"))
    except MarkerError:
        return None


def is_legacy_command(command: str) -> bool:
    return bool(_LEGACY_ASSIGNMENT_RE.match(command))


# -------------------------------------------------------------- marker-file


def marker_file_body(marker: Marker, source_sha256: str, runtime: str = "") -> str:
    """The `.nxt-managed` body: marker, source digest, and the runtime.

    The runtime line is what makes an owned directory sweepable. Without it
    the only path a marker file names is the directory the marker file is
    inside, which exists by definition -- so an extension whose interpreter or
    dispatcher is long gone could never be detected as an orphan.
    """
    body = f"{marker}\nsource={source_sha256}\n"
    return f"{body}runtime={runtime}\n" if runtime else body


def parse_marker_file(body: str) -> tuple[Marker, str, str]:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        raise MarkerError("empty marker file")
    marker = parse_marker(lines[0])
    fields = {"source": "", "runtime": ""}
    for line in lines[1:]:
        key, _, value = line.partition("=")
        if key in fields:
            fields[key] = value
    return marker, fields["source"], fields["runtime"]


# ------------------------------------------------------------- comment-line


def comment_prefix(suffix: str) -> str:
    try:
        return _COMMENT_PREFIX[suffix]
    except KeyError:
        raise MarkerError(
            f"no comment syntax known for {suffix!r}; a first-line marker in "
            "the wrong syntax breaks the file NxtOS just claimed to own"
        ) from None


def comment_line(marker: Marker, suffix: str) -> str:
    return f"{comment_prefix(suffix)} {marker}"


def parse_comment_line(text: str, suffix: str) -> Marker:
    first = text.split("\n", 1)[0].strip()
    prefix = comment_prefix(suffix)
    if not first.startswith(prefix):
        raise MarkerError("first line is not a comment")
    return parse_marker(first[len(prefix):])

