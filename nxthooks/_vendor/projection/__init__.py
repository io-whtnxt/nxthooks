"""One envelope for everything NxtOS writes into a file it does not own.

A *projection* is a marked stanza inside somebody else's config: a hook entry
in Claude Code's `settings.json`, an `mcp_servers` table in Codex's
`config.toml`, a plugin file under OpenCode's config root. Three components
grew three partial grammars for this and none of them can answer the question
that matters when things go wrong: what did NxtOS put here, and is it still
what NxtOS put here.

The inversion this library exists for: **the ledger is authoritative, the
in-file marker is corroboration.** A marker can be deleted by the harness (see
`detect.py` for two harnesses that provably delete unknown keys) or by a human
with an editor. If ownership were "starts with the marker", a deleted marker
would mean a projection that uninstall cannot see and therefore cannot remove
-- a stanza outliving the component that wrote it, failing open, silently.
The ledger survives that. The marker is what makes the file legible to a human
with `jq`, and what makes `orphans.py` able to strip a stanza with no
component installed and no ledger at all.

Six modules, no component imports. This library is a dependency of components,
never the reverse:

    envelope.py  the marker grammar and the four in-file forms
    ledger.py    the append-only family record
    write.py     detect-gated, locked, compare-and-swap, style-preserving
    detect.py    the adapter table: is this harness actually here
    verify.py    tri-state per slot, plus foreign-change and runtime presence
    orphans.py   strip by grammar alone, importing nothing

Nothing imports this yet. Porting NxtHooks onto it is a separate wave; a
library that ships unused is a library whose first consumer can still argue
with its shape.
"""

from __future__ import annotations

from .detect import (
    HARNESSES,
    Detection,
    Harness,
    detect,
    harness,
)
from .envelope import (
    FORM_COMMENT_LINE,
    FORM_ENV_PREFIX,
    FORM_LEDGER_ONLY,
    FORM_MARKER_FILE,
    FORMS,
    Marker,
    MarkerError,
    command_payload,
    comment_line,
    digest,
    marker_file_body,
    parse_comment_line,
    parse_command_marker,
    parse_marker,
    parse_marker_file,
    stamp_command,
)
from .ledger import (
    Record,
    append,
    ledger_path,
    live_slots,
    last_for_file,
    read,
    rebuild,
)
from .orphans import Orphan, find_orphans, strip_orphans
from .verify import (
    ALTERED,
    INTACT,
    MISSING,
    SlotReport,
    VerifyReport,
    verify,
)
from .write import (
    ConcurrentModification,
    JsonStyle,
    NotDetected,
    WriteOutcome,
    detect_json_style,
    dump_json,
    edit_json,
    edit_text,
    foreign_digest,
    locked,
)

__all__ = [
    "ALTERED",
    "ConcurrentModification",
    "Detection",
    "FORMS",
    "FORM_COMMENT_LINE",
    "FORM_ENV_PREFIX",
    "FORM_LEDGER_ONLY",
    "FORM_MARKER_FILE",
    "HARNESSES",
    "Harness",
    "INTACT",
    "JsonStyle",
    "MISSING",
    "Marker",
    "MarkerError",
    "NotDetected",
    "Orphan",
    "Record",
    "SlotReport",
    "VerifyReport",
    "WriteOutcome",
    "append",
    "command_payload",
    "comment_line",
    "detect",
    "detect_json_style",
    "digest",
    "dump_json",
    "edit_json",
    "edit_text",
    "find_orphans",
    "foreign_digest",
    "harness",
    "last_for_file",
    "ledger_path",
    "live_slots",
    "locked",
    "marker_file_body",
    "parse_command_marker",
    "parse_comment_line",
    "parse_marker",
    "parse_marker_file",
    "read",
    "rebuild",
    "stamp_command",
    "strip_orphans",
    "verify",
]
