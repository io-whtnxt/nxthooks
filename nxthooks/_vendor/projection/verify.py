"""Tri-state per slot, because two states is where the drift hides.

`intact`, `altered`, `missing`. Collapsing any pair of those produces exactly
the blindness this family keeps rediscovering: NxtHooks' `verify` checks that
a stanza is *present* and calls that healthy, so a stanza a human edited and a
stanza NxtOS wrote read the same, and a stanza the harness deleted reads as a
harness that was never wired.

Alongside the three, two facts that are not about our stanza at all and are
reported separately rather than folded in:

- `foreign_changed`: the remainder of the file, with every Nxt slot removed,
  no longer hashes to what it hashed when NxtOS last wrote. Recorded and
  reported, never auto-repaired -- `install` repairs, because `install` is
  idempotent by contract and `verify` is not allowed to surprise anyone.
- `runtime_present`: the absolute path the stanza depends on still exists. A
  projection that outlives its runtime is the dangerous case on pi and
  OpenCode, where a missing runner resolves to `null` and the adapter treats
  that as allow.

Exit codes: 0 everything intact, 1 anything altered or missing, 2 operational
(the ledger itself could not be read).

This module *reads* JSON, TOML and YAML, because a `ledger-only` slot in
Hermes' `config.yaml` or Codex's `config.toml` is a slot `verify` has to be
able to answer for. `write.py` writes only JSON and text: a round-trip TOML or
YAML writer is the component's job (NxtMCP's ruamel path) until one has been
verified to survive the harness's own writer, which for Codex's `mcp_servers`
region it has not -- see `detect.py`.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .envelope import (
    FORM_COMMENT_LINE,
    FORM_ENV_PREFIX,
    FORM_LEDGER_ONLY,
    FORM_MARKER_FILE,
    MarkerError,
    command_payload,
    digest,
    parse_command_marker,
    parse_comment_line,
    parse_marker_file,
)
from .ledger import Record, live_slots, read
from .orphans import resolve_pointer, walk_commands
from .write import canonical_json, foreign_digest

INTACT = "intact"
ALTERED = "altered"
MISSING = "missing"


@dataclass(frozen=True)
class SlotReport:
    component: str
    harness: str
    file: Path
    slot: str
    form: str
    state: str
    detail: str
    runtime: str = ""
    runtime_present: bool = True


@dataclass(frozen=True)
class VerifyReport:
    component: str
    slots: tuple[SlotReport, ...] = ()
    foreign_changed: dict[str, bool] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    operational: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        if self.operational:
            return 2
        if any(slot.state != INTACT for slot in self.slots):
            return 1
        return 0


def _document(path: Path):
    """The parsed file, or None when this library cannot read that format."""
    try:
        if path.suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
        if path.suffix == ".toml":
            return tomllib.loads(path.read_text(encoding="utf-8"))
        if path.suffix in (".yaml", ".yml"):
            return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
        return None
    return None


def _check_env_prefix(record: Record, doc) -> tuple[str, str]:
    at_pointer = None
    for pointer, command in walk_commands(doc):
        marker = parse_command_marker(command)
        if marker is not None and str(marker) == record.marker:
            payload_sha = digest(command_payload(command))
            if payload_sha == record.own_sha256:
                if pointer != record.slot:
                    return INTACT, f"moved to {pointer} by the harness"
                return INTACT, ""
            return ALTERED, f"marker at {pointer} no longer covers its command"
        if pointer == record.slot:
            at_pointer = command
    if at_pointer is None:
        return MISSING, "no stanza carries this marker"
    if digest(command_payload(at_pointer)) == record.own_sha256:
        return ALTERED, f"command intact at {record.slot} but the marker is gone"
    return ALTERED, f"a foreign command now occupies {record.slot}"


def _check_marker_file(record: Record, path: Path) -> tuple[str, str]:
    try:
        marker, source, _ = parse_marker_file(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return MISSING, f"{path} is unreadable"
    except MarkerError as exc:
        return ALTERED, f"{path} is not a marker file ({exc})"
    if str(marker) != record.marker:
        return ALTERED, f"{path} carries {marker}, not {record.marker}"
    if source != record.own_sha256:
        return ALTERED, f"{path} records a different source digest"
    return INTACT, ""


def _check_comment_line(record: Record, path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return MISSING, f"{path} is unreadable"
    try:
        marker = parse_comment_line(text, path.suffix)
    except MarkerError as exc:
        return ALTERED, f"{path} no longer opens with a marker ({exc})"
    if str(marker) != record.marker:
        return ALTERED, f"{path} carries a different marker"
    _, _, rest = text.partition("\n")
    if digest(rest) != record.own_sha256:
        return ALTERED, f"{path} has been edited since NxtOS wrote it"
    return INTACT, ""


def _unreadable(path: Path) -> tuple[str, str]:
    """A format this library cannot parse. Not `missing`: nothing was looked
    at, and reporting absence from a file that was never read is a lie the
    ledger would then be blamed for."""
    return ALTERED, f"{path} is a format this library cannot read"


def _check_ledger_only(record: Record, doc) -> tuple[str, str]:
    value = resolve_pointer(doc, record.slot)
    if value is None:
        return MISSING, f"nothing at {record.slot}"
    if digest(canonical_json(value)) != record.own_sha256:
        return ALTERED, f"the value at {record.slot} is not what NxtOS wrote"
    return INTACT, ""


def _foreign_now(path: Path, records: list[Record]):
    """The file with every Nxt slot of every live record removed.

    Only meaningful for a file NxtOS shares with a harness. A file NxtOS owns
    outright has no foreign remainder, and reporting one for it would invent a
    fact.
    """
    from .orphans import prune

    doc = _document(path)
    if doc is None:
        return None
    pointers = []
    for pointer, command in walk_commands(doc):
        if parse_command_marker(command) is not None:
            pointers.append(pointer)
    pointers.extend(
        r.slot for r in records
        if r.form == FORM_LEDGER_ONLY and resolve_pointer(doc, r.slot) is not None
    )
    for pointer in sorted(set(pointers), reverse=True):
        prune(doc, pointer)
    return foreign_digest(doc, json_like=True)


def verify(component: str, *, home: Path | str | None = None,
           env: dict | None = None,
           path: Path | str | None = None) -> VerifyReport:
    _, warnings = read(path=path, home=home, env=env)
    operational = tuple(w for w in warnings if "unknown fields" not in w)
    records = live_slots(component, path=path, home=home, env=env)

    by_file: dict[str, list[Record]] = {}
    for record in records:
        by_file.setdefault(record.file, []).append(record)

    slots: list[SlotReport] = []
    foreign_changed: dict[str, bool] = {}
    for file_name, file_records in sorted(by_file.items()):
        file_path = Path(file_name)
        doc = _document(file_path)

        for record in file_records:
            runtime_present = not record.runtime or Path(record.runtime).exists()
            if not file_path.exists():
                state, detail = MISSING, f"{file_path} does not exist"
            elif record.form == FORM_ENV_PREFIX:
                state, detail = (
                    (MISSING, f"{file_path} is not readable JSON") if doc is None
                    else _check_env_prefix(record, doc)
                )
            elif record.form == FORM_MARKER_FILE:
                state, detail = _check_marker_file(record, file_path)
            elif record.form == FORM_COMMENT_LINE:
                state, detail = _check_comment_line(record, file_path)
            else:
                state, detail = (
                    _unreadable(file_path) if doc is None
                    else _check_ledger_only(record, doc)
                )
            slots.append(SlotReport(
                component=record.component, harness=record.harness,
                file=file_path, slot=record.slot, form=record.form,
                state=state, detail=detail, runtime=record.runtime,
                runtime_present=runtime_present,
            ))

        shared = [r for r in file_records if r.form in (FORM_ENV_PREFIX, FORM_LEDGER_ONLY)]
        last_written = next(
            (r.foreign_sha256_after for r in reversed(file_records)
             if r.foreign_sha256_after), ""
        )
        if shared and last_written and file_path.exists():
            current = _foreign_now(file_path, file_records)
            foreign_changed[file_name] = (
                current is None or current != last_written
            )

    return VerifyReport(
        component=component, slots=tuple(slots),
        foreign_changed=foreign_changed, warnings=warnings,
        operational=operational,
    )
