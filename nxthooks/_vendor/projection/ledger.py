"""The append-only family record, and the authority for wire, unwire, verify.

One file for the whole family, not one per component, because the question
nobody could answer before was a cross-component one: two Nxt writers meeting
on `~/.claude/settings.json` have to find each other somewhere, and a
per-component ledger is exactly as blind as no ledger at all.

Authoritative, with the in-file marker as corroboration. The inversion earns
its keep in the one case that matters: a marker deleted by the harness or by a
human. If ownership were "starts with the marker", that stanza is now
invisible to uninstall and outlives its component. With the ledger
authoritative it is `missing`, which is a reportable state, and `unwire` still
knows a slot was there.

Recoverable rather than sacred: `rebuild` reconstructs records from the
in-file markers for the forms that carry them, so a deleted ledger is a
degraded state and not a lost one.

One field beyond the plan's record: `form`. Without it `verify` cannot know
whether to look for an env-prefix on a command string or a `.nxt-managed`
file, and would have to guess from the path -- which is the kind of inference
that makes a tri-state collapse into two.
"""

from __future__ import annotations

import functools
import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from nxthooks._vendor import paths

from .envelope import FORM_LEDGER_ONLY, FORMS
from .write import locked

LEDGER_NAME = "projections.jsonl"

WIRE = "wire"
UNWIRE = "unwire"
VERIFY = "verify"
REPAIR = "repair"
OPS = (WIRE, UNWIRE, VERIFY, REPAIR)

#: Ops after which a slot is live. `verify` records an observation and must
#: not resurrect a slot that `unwire` removed.
_LIVE_OPS = (WIRE, REPAIR)


@dataclass(frozen=True)
class Record:
    op: str
    component: str
    version: str
    harness: str
    file: str
    slot: str
    marker: str = ""
    form: str = FORM_LEDGER_ONLY
    own_sha256: str = ""
    foreign_sha256_before: str = ""
    foreign_sha256_after: str = ""
    runtime: str = ""
    ts: float = field(default_factory=time.time)
    #: N7. Which shape of NxtOS wrote this slot. A projection embeds an
    #: absolute runtime path, so a slot wired from `dev` points at whatever
    #: branch a checkout has out; recording the shape is what turns that from
    #: an invisible mix into a finding `verify` can name.
    shape: str = field(default_factory=lambda: _shape())

    @property
    def key(self) -> tuple[str, str]:
        return (self.file, self.slot)


@functools.lru_cache(maxsize=1)
def _shape() -> str:
    """The running shape, read once per process.

    Cached because every record construction would otherwise re-read
    distribution metadata, and the shape cannot change inside one process:
    the interpreter is what determines it.
    """
    return paths.install_shape().shape


def ledger_path(home: Path | str | None = None, env: dict | None = None) -> Path:
    return paths.state_home(env, home) / LEDGER_NAME


def append(record: Record, *, path: Path | str | None = None,
           home: Path | str | None = None, env: dict | None = None) -> Path:
    """One line, under the family lock, with `O_APPEND`.

    Append-only is what makes this readable after a crash: a torn final line
    costs the last record, not the file.
    """
    if record.op not in OPS:
        raise ValueError(f"unknown ledger op {record.op!r}")
    if record.form not in FORMS:
        raise ValueError(
            f"unknown projection form {record.form!r}; a slot whose form the "
            "reader cannot dispatch on is a slot verify has to guess about"
        )
    target = Path(path) if path else ledger_path(home, env)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(record), sort_keys=True, ensure_ascii=False) + "\n"
    with locked(target):
        handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(handle, line.encode("utf-8"))
            os.fsync(handle)
        finally:
            os.close(handle)
    return target


def read(*, path: Path | str | None = None, home: Path | str | None = None,
         env: dict | None = None) -> tuple[tuple[Record, ...], tuple[str, ...]]:
    """Every record, plus what could not be read.

    Unreadable lines are reported rather than swallowed. A silently dropped
    line is a silently dropped slot, and a slot nothing knows about is the
    exact failure this whole library is for.
    """
    target = Path(path) if path else ledger_path(home, env)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (), ()
    known = {f.name for f in fields(Record)}
    records: list[Record] = []
    warnings: list[str] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"{target}:{number} is not JSON ({exc.msg})")
            continue
        if not isinstance(payload, dict):
            warnings.append(f"{target}:{number} is not an object")
            continue
        unknown = set(payload) - known
        if unknown:
            warnings.append(
                f"{target}:{number} carries unknown fields "
                f"{', '.join(sorted(unknown))}; written by a newer NxtOS"
            )
        try:
            records.append(Record(**{k: v for k, v in payload.items() if k in known}))
        except TypeError as exc:
            warnings.append(f"{target}:{number} is not a record ({exc})")
    return tuple(records), tuple(warnings)


def last_for_file(file: Path | str, *, path: Path | str | None = None,
                  home: Path | str | None = None,
                  env: dict | None = None) -> Record | None:
    wanted = str(Path(file).expanduser())
    records, _ = read(path=path, home=home, env=env)
    for record in reversed(records):
        if record.file == wanted:
            return record
    return None


def foreign_moved(file: Path | str, foreign_sha256: str, *,
                  path: Path | str | None = None,
                  home: Path | str | None = None,
                  env: dict | None = None) -> bool:
    """Did the harness rewrite its own file since NxtOS last wrote it?

    No prior record is not a move: a file NxtOS has never written cannot have
    drifted from a state NxtOS never recorded.
    """
    previous = last_for_file(file, path=path, home=home, env=env)
    if previous is None or not previous.foreign_sha256_after:
        return False
    return previous.foreign_sha256_after != foreign_sha256


def live_slots(component: str | None = None, *, path: Path | str | None = None,
               home: Path | str | None = None,
               env: dict | None = None) -> tuple[Record, ...]:
    """The current wire state, folded from the log.

    Last write wins per `(file, slot)`; a slot whose last write was an unwire
    is gone. Ordering is by position in the file, not by `ts`, because a clock
    that steps backwards must not resurrect a projection.
    """
    records, _ = read(path=path, home=home, env=env)
    state: dict[tuple[str, str], Record] = {}
    for record in records:
        if record.op == VERIFY:
            continue
        state[record.key] = record
    live = [r for r in state.values() if r.op in _LIVE_OPS]
    if component is not None:
        live = [r for r in live if r.component == component]
    return tuple(sorted(live, key=lambda r: (r.file, r.slot)))


def conflicting_slot(component: str, file: Path | str, slot: str, *,
                     path: Path | str | None = None,
                     home: Path | str | None = None,
                     env: dict | None = None) -> Record | None:
    """Another component's live record on the same slot.

    Disjoint slots in one file are normal and expected; the same slot is two
    components fighting over one stanza, and the wire step refuses with both
    names rather than letting the later writer win silently.
    """
    wanted = (str(Path(file).expanduser()), slot)
    for record in live_slots(path=path, home=home, env=env):
        if record.key == wanted and record.component != component:
            return record
    return None


def rebuild(*, home: Path | str | None = None, env: dict | None = None,
            path: Path | str | None = None) -> tuple[Record, ...]:
    """Reconstruct records from in-file markers, for the forms that carry one.

    What makes the ledger recoverable rather than sacred. It cannot recover a
    `ledger-only` slot -- there is nothing in the file to recover it from -- so
    those stay lost, and saying so here is more useful than a rebuild that
    quietly returns a partial answer as if it were whole.
    """
    from .orphans import scan  # local: orphans reads the ledger for runtimes

    target = Path(path) if path else ledger_path(home, env)
    recovered: list[Record] = []
    for finding in scan(home=home, env=env):
        record = Record(
            op=WIRE,
            component=finding.marker.component,
            version=finding.marker.version,
            harness=finding.harness,
            file=str(finding.file),
            slot=finding.slot,
            marker=str(finding.marker),
            form=finding.form,
            own_sha256=finding.own_sha256,
            runtime=finding.runtime,
            # Empty, not the shape running the rebuild. The in-file marker
            # carries no shape, so a rebuilt record cannot know which one
            # wrote the slot, and stamping the current one would turn "we do
            # not know" into a claim that reads exactly like knowledge.
            shape="",
        )
        recovered.append(record)
        append(record, path=target)
    return tuple(recovered)
