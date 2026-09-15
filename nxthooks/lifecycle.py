"""NxtHooks lifecycle: install / uninstall / verify / doctor.

Everything NxtHooks writes into a file it does not own goes through
`nxthooks._vendor.projection`. That is not a refactor for tidiness; three concrete
defects lived in the serialiser this replaces.

**Presence, never config.** `cmd_install` used to select harnesses from
`[harnesses]` in the config file, and the creation of a harness home was
incidental to how each transport installed: `copytree` and `mkdir(parents=
True)` both build missing parents. So a stranger's Claude-Code-only machine
came out of `nxthooks install` holding a `~/.pi` and a `~/.config/opencode`
it had never had, and the release definition of done says in as many words
that it must not. Selection is now `projection.detect`'s: the harness's
binary on PATH, or its config root holding one of *its own* files. The
config's `[harnesses]` block records what was detected; it no longer decides.

**Install and verify agreed on nothing.** `cmd_install` read `load_config()`
*before* `seed_config()` wrote the seed, so the first install on a fresh
machine wired the built-in defaults while `cmd_verify`, running seconds later
against the now-seeded config, reported gaps for the harnesses install had
deliberately skipped. A stranger's first install always ended exit 1. Both
sides now ask `detect`, and the seed is written before anything reads it.

**`json.dumps(data, indent=2)`.** Python's default is `ensure_ascii=True`, so
every wire rewrote a stranger's accented text into `\\uXXXX` escapes, and
every later foreign-change check saw a file that had moved. The projection
writer takes `ensure_ascii` from whether the file was ASCII to begin with,
preserves the detected indent, locks, and compare-and-swaps.

Ownership is a ledger slot with a hash (`~/.local/state/nxt/projections.jsonl`),
corroborated by an in-file marker where the format has an inert slot. The
`installed-roots` file this package used to write is superseded: the roots a
previous install wired are read back out of the ledger's `runtime` field. It
is still *read* where an older install left one behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from nxthooks._vendor import paths
from nxthooks._vendor.projection import (
    FORM_COMMENT_LINE,
    FORM_ENV_PREFIX,
    FORM_MARKER_FILE,
    Detection,
    Record,
    command_payload,
    detect,
    digest,
    edit_json,
    NotDetected,
    harness as harness_spec,
    marker_file_body,
    parse_command_marker,
    stamp_command,
)
from nxthooks._vendor.projection.envelope import (
    LEGACY_ENV_PREFIX,
    MARKER_FILE_NAME,
    build,
    is_legacy_command,
)
from nxthooks._vendor.projection.ledger import UNWIRE, WIRE, append as ledger_append, live_slots
from nxthooks._vendor.projection.orphans import runtime_of, walk_commands
from nxthooks._vendor.projection.verify import ALTERED, INTACT, MISSING, verify as verify_slots

from . import HARNESSES, __version__
from .operation_lock import exclusive_lifecycle
from .config import load_config, resolve_root, user_config_path
from .manifest import load_manifests
from .registry import builtin_manifests, render_collisions
from .platform import COMPONENT, cache_dir, config_dir, state_dir

#: What NxtHooks 0.1 wrote instead of a marker. Recognised on unwire through
#: 0.4 so an upgrade can still clean up after the writer it replaced.
MARKER = LEGACY_ENV_PREFIX

EVENT_TO_CC = {
    "session_start": "SessionStart",
    "prompt_submit": "UserPromptSubmit",
    "before_tool": "PreToolUse",
    "after_tool": "PostToolUse",
}

EVENT_TO_CODEX = {
    **EVENT_TO_CC,
    "stop": "Stop",
    "session_end": "SessionEnd",
}


class CodexHooksError(ValueError):
    """The Codex hook layer is present but unsafe to rewrite."""


def _pkg_dir() -> Path:
    return Path(__file__).resolve().parent


def _source_provenance(src: Path) -> str:
    """`src`, plus the branch when it is a git working tree.

    Which source install ships is decided by sys.path, so it follows the
    caller's working directory: a checkout wins over site-packages. That is
    the right rule -- install should ship the code it is running -- but it was
    invisible, and on 2026-09-13 it twice replaced a good runtime with one
    from a branch that predated the `codex` harness. Every Codex tool call
    then came back "Blocked by hook" and neither install's output named a
    source.

    A checkout is the ambiguous case, so the branch is part of the answer.
    """
    try:
        inside = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if inside != "true":
            return str(src)
        branch = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return f"{src} (git checkout, branch {branch})" if branch else str(src)
    except (OSError, subprocess.SubprocessError):
        return str(src)


def _python() -> str:
    return sys.executable or "python3"


# ------------------------------------------------------------------- host


#: The pane over this CLI, installed by `nxthooks install` and removed by
#: `nxthooks uninstall`. See `nxthooks._vendor.plugins.delivery` for why it travels
#: with the component instead of with the host.
PLUGIN_MANIFEST = Path(__file__).resolve().parent / "plugin.yaml"
PLUGIN_ID = "nxthooks"


@dataclass(frozen=True)
class Host:
    """The machine being installed onto, as data rather than as `Path.home()`.

    Every path NxtHooks reads or writes hangs off this, which is what makes a
    sandbox HOME and a packaged install the same code path. It is also the
    only thing standing between a test suite and the operator's real
    `~/.claude/settings.json`: a function that spells `Path.home()` inside
    itself cannot be pointed anywhere else, and this package used to be built
    entirely out of those.

    `path_lookup` is threaded through to `detect` so a sandbox can answer the
    binary-on-PATH half without the test machine's real PATH deciding it.
    """

    home: Path
    env: dict
    path_lookup: Callable[[str], str | None] | None = None
    #: Harnesses the operator named on the command line. `--harnesses pi` on a
    #: machine with no pi is an assertion, and this is where it is recorded --
    #: as a `Detection` carrying the operator as its reason, not as a bypass
    #: of the detect gate. Nothing else can produce one.
    forced: frozenset = frozenset()

    @classmethod
    def local(cls) -> "Host":
        return cls(Path.home(), dict(os.environ))

    def detect(self, harness_id: str) -> Detection:
        found = detect(harness_id, home=self.home, env=self.env,
                       path_lookup=self.path_lookup)
        if found.present or harness_id not in self.forced:
            return found
        return Detection(
            harness=harness_id, present=True,
            reason="named explicitly with --harnesses, not detected",
            root=found.root,
        )

    def harness_root(self, harness_id: str) -> Path:
        return harness_spec(harness_id).root_path(self.home, self.env)

    @property
    def cc_settings(self) -> Path:
        return self.harness_root("claude-code") / "settings.json"

    @property
    def codex_hooks(self) -> Path:
        return self.harness_root("codex") / "hooks.json"

    @property
    def pi_extension(self) -> Path:
        return self.harness_root("pi") / "agent" / "extensions" / "nxthooks"

    @property
    def oc_plugin(self) -> Path:
        return self.harness_root("opencode") / "plugins" / "nxthooks.ts"

    @property
    def hermes_plugin(self) -> Path:
        return self.harness_root("hermes") / "plugins" / "nxthooks"

    @property
    def config_dir(self) -> Path:
        return config_dir(self.env, self.home)

    @property
    def state_dir(self) -> Path:
        return state_dir(self.env, self.home)

    @property
    def cache_dir(self) -> Path:
        return cache_dir(self.env, self.home)

    @property
    def hooks_d(self) -> Path:
        return self.config_dir / "hooks.d"

    @property
    def config_path(self) -> Path:
        return user_config_path(self.env, self.home)

    def config(self, cwd: str | None = None) -> dict:
        return load_config(cwd, self.env, self.home)

    def root(self, cfg: dict | None = None) -> Path:
        return resolve_root(self.config() if cfg is None else cfg, self.env, self.home)


# ------------------------------------------------- directories we created


#: Paths `install` had to bring into existence, recorded so `uninstall` can
#: remove exactly those and nothing else. Directories mostly, plus a harness
#: file that did not exist before we wrote our first stanza into it. Lives in
#: the config dir rather than in state, because a plain `uninstall` keeps the
#: config dir and a second `uninstall --purge-config` still needs the list.
CREATED_PATHS = "created-paths"

#: Where the partial `nxthooks._vendor` the runtime carries lives, under `lib/`. Its
#: own directory rather than `lib/nxthooks._vendor` so it can be appended to
#: `sys.path` instead of prepended -- see `install_runtime`.
FRAMEWORK_DIR = "_framework"

#: What `install` wrote and `uninstall` reads to know which files are ours.
RUNTIME_INVENTORY = ".nxthooks-runtime.json"


def missing_ancestors(path: Path, stop: Path) -> list[Path]:
    """The directories between `stop` and `path` that do not exist yet.

    Called before the `mkdir`, so what it returns is exactly what the `mkdir`
    is about to create. Shallowest first.
    """
    missing: list[Path] = []
    current = Path(path)
    stop = Path(stop)
    while current != stop and current != current.parent:
        if current.exists():
            break
        missing.append(current)
        current = current.parent
    return list(reversed(missing))


def _read_created(host: Host) -> list[Path]:
    try:
        lines = (host.config_dir / CREATED_PATHS).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [Path(line) for line in (raw.strip() for raw in lines) if line]


def _record_created(host: Host, made: list[Path]) -> None:
    """Append-unique. Two installs must not make the second one forget what
    the first one built."""
    if not made:
        return
    known = _read_created(host)
    merged = list(dict.fromkeys(str(p) for p in (*known, *made)))
    target = host.config_dir / CREATED_PATHS
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(merged) + "\n", encoding="utf-8")


def make_dir(host: Host, path: Path) -> list[Path]:
    """`mkdir -p`, recording what it had to build. Returns those directories."""
    made = missing_ancestors(path, host.home)
    path.mkdir(parents=True, exist_ok=True)
    return made


def prune_created(host: Host, candidates: list[Path]) -> list[Path]:
    """Remove each recorded path that is now empty, deepest first.

    Empty is the guard against removing something NxtOS did not create: the
    record says we brought it into existence, and emptiness says nothing has
    moved in since. For a directory that is `rmdir`; for a harness file we
    authored, it is a document that unwiring left with nothing in it. A file
    the operator has since put something in is kept, and `$HOME` itself is
    never a candidate.
    """
    removed: list[Path] = []
    for path in sorted(set(candidates), key=lambda p: len(p.parts), reverse=True):
        if path == host.home:
            continue
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                continue
            removed.append(path)
        elif path.is_file() and _is_empty_document(path):
            path.unlink()
            removed.append(path)
    return removed


def _is_empty_document(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if not text.strip():
        return True
    try:
        return json.loads(text) == {}
    except json.JSONDecodeError:
        return False


# ------------------------------------------------------------- ownership


def _known_roots(host: Host) -> tuple[Path, ...] | None:
    """Every install root whose stanzas this machine is allowed to strip.

    The ledger's `runtime` is the dispatcher script, `<root>/bin/*-dispatch.sh`,
    so the roots fall out of it. An older install may have left an
    `installed-roots` file; it is read, never written.

    `None` means "no history at all", and is the deliberate fallback for a
    machine that predates both records: there, the configured root may already
    have drifted from whatever wired the live stanzas, so the narrow
    root-scoped strip could leave one wired against a script uninstall is
    about to delete.
    """
    roots: list[Path] = []
    for record in live_slots(COMPONENT, home=host.home, env=host.env):
        if record.form != FORM_ENV_PREFIX or not record.runtime:
            continue
        candidate = Path(record.runtime).parent.parent
        if candidate not in roots:
            roots.append(candidate)
    legacy = host.config_dir / "installed-roots"
    try:
        lines = legacy.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for raw in lines:
        candidate = Path(raw.strip()).expanduser()
        if raw.strip() and candidate.is_absolute() and candidate not in roots:
            roots.append(candidate)
    return tuple(roots) or None


def _under(path: Path, root: Path) -> bool:
    """Unresolved containment. A wired stanza embeds the root exactly as it
    was configured, so resolving here (through a symlinked install root)
    would silently stop matching the very stanzas it is meant to find."""
    return path == root or root in path.parents


def owned_command(command: str, roots: tuple[Path, ...] | None) -> bool:
    """Is this stanza ours, and from an install this machine knows about?

    Both grammars count: an `NXT=hooks/<version>/<sha12>` marker naming this
    component, and NxtHooks 0.1's `NXTHOOKS_MANAGED=1`. Neither is trusted
    beyond identification -- the legacy prefix names no component, no version
    and no hash, which is the whole argument for having replaced it.
    """
    marker = parse_command_marker(command)
    mine = marker is not None and marker.component == COMPONENT
    if not mine and not is_legacy_command(command):
        return False
    if roots is None:
        return True
    runtime = runtime_of(command_payload(command))
    return bool(runtime) and any(_under(Path(runtime), root) for root in roots)


def strip_ours(doc: dict, roots: tuple[Path, ...] | None) -> dict:
    """Remove our stanzas, then the groups and events they emptied.

    Filtering rather than pruning by JSON pointer: pointers into a list are
    positional, and removing `/hooks/PreToolUse/2` before `/hooks/PreToolUse/10`
    (which is the order a reverse string sort gives) deletes the wrong entry.
    """
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return doc
    removed = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            kept = [
                hook for hook in (group.get("hooks") or [])
                if not (isinstance(hook, dict)
                        and owned_command(hook.get("command", ""), roots))
            ]
            if len(kept) != len(group.get("hooks") or []):
                removed = True
            if kept:
                group["hooks"] = kept
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if removed and not hooks:
        # An empty `hooks` map is residue we put there. Converge on the file
        # as it was, not on the file plus a key that means nothing. Only when
        # we emptied it: an operator's own empty map is theirs to keep.
        del doc["hooks"]
    return doc


def _remainder(roots: tuple[Path, ...] | None):
    """The foreign-digest callback. Deep-copies: `strip_ours` mutates, and the
    writer takes this digest over the document it is about to hand to
    `mutate`."""
    def remainder(doc):
        return strip_ours(json.loads(json.dumps(doc)), roots)
    return remainder


# --------------------------------------------------------------- CC wiring


def _require_present(host: Host, harness_id: str) -> Detection:
    """The gate, before the `mkdir`.

    `edit_json` refuses an undetected write too, but only after this module
    has already built the directory the file was going to live in -- and a
    `~/.pi` that exists because a write was attempted and refused is exactly
    the residue the detect rule is about.
    """
    detection = host.detect(harness_id)
    if not detection.present:
        raise NotDetected(
            f"refusing to wire {harness_id}: {detection.reason}"
        )
    return detection


def _source_state(entry: dict) -> str:
    """`installed` | `pending` | `missing` for one hook's implementation."""
    if Path(os.path.expanduser(entry["source"])).is_file():
        return "installed"
    return "pending" if entry.get("provider") else "missing"


def _check_source(entry: dict) -> bool:
    """A missing hook source is only ever this component's failure when
    nothing else claims it.

    A manifest carrying `provider` names a separate installer -- NxtOS for
    the library tier -- that NxtHooks does not control and cannot repair.
    Reporting that as GAP told the operator to run `nxthooks install`,
    which cannot install a source another component owns; a remedy that
    cannot work trains its reader to stop reading the report. So a missing
    provided source reports `[pend]` and does not count against the
    GAPS-FOUND exit. A missing source naming no provider is still ours to
    answer for, and still fails loudly.
    """
    label = f"hook '{entry['name']}' source"
    src = Path(os.path.expanduser(entry["source"]))
    state = _source_state(entry)
    if state == "installed":
        return _check(label, True, str(src))
    if state == "pending":
        print(f"  [pend] {label}  {src} (provided by {entry['provider']}, not installed yet)")
        return True
    return _check(label, False, str(src))




def matcher_groups_for(event: str, host: Host | None = None) -> list[str | None]:
    """Tool-name matchers declared by the live hooks for one event.

    Scoping the harness-side stanza to the tools that actually have a hook
    keeps the dispatcher from being spawned on every unrelated tool call just
    to decide it has nothing to run. Three distinct answers, and the empty one
    is the one that used to be lost:

    - `[]`      no live hook declares this event -- wire nothing.
    - `[None]`  at least one hook declares it and matches every tool.
    - `[...]`   the union of the tool globs declared for it.

    A blanket `[None]` fallback for the empty case wired a match-all stanza for
    an event with nothing behind it, which is exactly what this scoping exists
    to prevent.
    """
    groups: set[str] = set()
    match_all = False
    for entry in load_manifests(host.hooks_d if host else None):
        if event not in (entry.get("events") or []):
            continue
        if not entry.get("enabled", True):
            continue
        matchers = entry.get("matchers") or []
        if matchers:
            groups.update(matchers)
        else:
            match_all = True
    if match_all:
        return [None]
    return sorted(groups)


def wiring_groups(host: Host | None = None) -> dict[str, list[str | None]]:
    """What to wire per event, for a harness that scopes by tool name.

    session_start and prompt_submit carry no tool, so a matcher there would
    never match anything: they wire one match-all stanza if any hook declares
    them, and nothing if none does.
    """
    groups: dict[str, list[str | None]] = {}
    for canon in EVENT_TO_CC:
        declared = matcher_groups_for(canon, host)
        if canon in ("session_start", "prompt_submit"):
            groups[canon] = [None] if declared else []
        else:
            groups[canon] = declared
    return groups


#: Status text per canonical event. PostToolUse and the rest carry none:
#: the tool has already run, so there is nothing to tell the user is pending.
_CC_STATUS = {
    "session_start": "NxtHooks session context...",
    "prompt_submit": "Running NxtHooks checks...",
    "before_tool": "Running NxtHooks guards...",
    "after_tool": "",
}


def _cc_plan(root: Path, groups_by_event: dict[str, list[str | None]]):
    """One stanza per (event, matcher) that some live hook actually declares.

    Two defects closed at once. PostToolUse was never in this plan -- it is in
    EVENT_TO_CC and in _CODEX_PLAN, but the Claude Code plan hardcoded three
    events -- so an after_tool hook never fired on Claude Code at all. NxtLint,
    the one builtin, declares exactly that event.

    And an event with no hook behind it now gets no stanza, instead of a
    match-all one that spent `sh -> python -> import -> exit` on every session,
    prompt and tool call to discover there was nothing to run.
    """
    script = root / "bin" / "cc-dispatch.sh"
    plan = []
    for canon, cc_event in EVENT_TO_CC.items():
        for matcher in groups_by_event.get(canon) or []:
            plan.append((cc_event, matcher, canon, _CC_STATUS.get(canon, "")))
    return script, plan


def _record_slots(host: Host, harness: str, file: Path, doc: dict, script: Path,
                  outcome) -> int:
    """One ledger record per stanza that carries our marker, keyed on the
    pointer it actually landed at in the document we just wrote."""
    written = 0
    for pointer, command in walk_commands(doc):
        marker = parse_command_marker(command)
        if marker is None or marker.component != COMPONENT:
            continue
        ledger_append(
            Record(
                op=WIRE, component=COMPONENT, version=__version__,
                harness=harness, file=str(file), slot=pointer,
                marker=str(marker), form=FORM_ENV_PREFIX,
                own_sha256=digest(command_payload(command)),
                foreign_sha256_before=outcome.foreign_before,
                foreign_sha256_after=outcome.foreign_after,
                runtime=str(script),
            ),
            home=host.home, env=host.env,
        )
        written += 1
    return written


def _unrecord_slots(host: Host, harness: str, file: Path, outcome) -> None:
    for record in live_slots(COMPONENT, home=host.home, env=host.env):
        if record.harness != harness or record.file != str(file):
            continue
        ledger_append(
            Record(
                op=UNWIRE, component=COMPONENT, version=__version__,
                harness=harness, file=record.file, slot=record.slot,
                marker=record.marker, form=record.form,
                own_sha256=record.own_sha256,
                foreign_sha256_before=outcome.foreign_before,
                foreign_sha256_after=outcome.foreign_after,
                runtime=record.runtime,
            ),
            home=host.home, env=host.env,
        )


def cc_wire(host: Host, root: Path, groups_by_event: dict[str, list[str | None]]) -> list[str]:
    """Converge Claude Code settings onto exactly the current stanza set.

    Stripped then rewritten from live state, so re-running install always
    converges. A stanza wired by an install root this machine has no record of
    is left alone: it belongs to somebody else's NxtHooks.
    """
    detection = _require_present(host, "claude-code")
    roots = _known_roots(host)
    roots = tuple({*(roots or ()), root})
    script, plan = _cc_plan(root, groups_by_event)
    landed: dict = {}

    def mutate(doc: dict) -> dict:
        strip_ours(doc, roots)
        section = doc.setdefault("hooks", {})
        for cc_event, matcher, canonical, status in plan:
            entry = {
                "type": "command",
                "command": stamp_command(f'sh "{script}" {canonical}',
                                         COMPONENT, __version__),
                "statusMessage": status,
            }
            stanza = ({"matcher": matcher, "hooks": [entry]} if matcher
                      else {"hooks": [entry]})
            section.setdefault(cc_event, []).append(stanza)
        landed["doc"] = doc
        return doc

    made = make_dir(host, host.cc_settings.parent)
    if not host.cc_settings.exists():
        # A harness can be present on its binary alone. The settings file we
        # then author is ours to take away again; `{}` left on a stranger's
        # disk is residue like any other.
        made.append(host.cc_settings)
    outcome = edit_json(host.cc_settings, detection=detection, mutate=mutate,
                        strip=_remainder(roots))
    _record_created(host, made)
    count = _record_slots(host, "claude-code", host.cc_settings, landed["doc"],
                          script, outcome)
    return [f"claude-code wired     {host.cc_settings} ({count} stanzas)"]


def cc_unwire(host: Host, roots: tuple[Path, ...] | None) -> list[str]:
    detection = host.detect("claude-code")
    if not detection.present or not host.cc_settings.exists():
        return []
    outcome = edit_json(host.cc_settings, detection=detection,
                        mutate=lambda doc: strip_ours(doc, roots),
                        strip=_remainder(roots))
    _unrecord_slots(host, "claude-code", host.cc_settings, outcome)
    return ["claude-code unwired  removed all NxtHooks-managed stanzas"] if outcome.changed else []


# ------------------------------------------------------------ Codex wiring


def _validate_codex(host: Host) -> None:
    """Refuse to rewrite a Codex hook layer we cannot read as one.

    `edit_json` already refuses a file that is not JSON. This is the shape
    check on top: a `hooks` value that is not an object, or an event whose
    groups are not a list, means somebody's hand-written file that a converge
    pass would silently reinterpret.
    """
    try:
        data = json.loads(host.codex_hooks.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except json.JSONDecodeError as exc:
        raise CodexHooksError(
            f"invalid Codex hooks JSON at {host.codex_hooks}; refusing to "
            f"overwrite: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise CodexHooksError(
            f"invalid Codex hooks shape at {host.codex_hooks}; refusing to "
            "overwrite: root must be an object"
        )
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise CodexHooksError(
            f"invalid Codex hooks shape at {host.codex_hooks}; refusing to "
            "overwrite: hooks must be an object"
        )
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise CodexHooksError(
                f"invalid Codex hooks shape at {host.codex_hooks}; refusing to "
                f"overwrite: {event} must be a list"
            )
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                raise CodexHooksError(
                    f"invalid Codex hooks shape at {host.codex_hooks}; refusing "
                    f"to overwrite: {event} has an invalid group"
                )


#: Codex caps SessionEnd hooks at three seconds.
_CODEX_PLAN = (
    ("SessionStart", "session_start", "NxtHooks session context...", 30),
    ("UserPromptSubmit", "prompt_submit", "Running NxtHooks checks...", 30),
    ("PreToolUse", "before_tool", "Running NxtHooks guards...", 30),
    ("PostToolUse", "after_tool", "", 30),
    ("Stop", "stop", "", 30),
    ("SessionEnd", "session_end", "", 3),
)


def codex_wire(host: Host, root: Path) -> list[str]:
    """Converge the user Codex hook layer without touching foreign hooks.

    One catch-all tool stanza is intentional. Codex aliases apply_patch to
    Edit and Write while the dispatcher also filters manifests; separate
    matcher groups could therefore run the same guard twice concurrently.
    """
    _validate_codex(host)
    detection = _require_present(host, "codex")
    roots = _known_roots(host)
    roots = tuple({*(roots or ()), root})
    script = root / "bin" / "codex-dispatch.sh"
    landed: dict = {}

    def mutate(doc: dict) -> dict:
        strip_ours(doc, roots)
        section = doc.setdefault("hooks", {})
        for event, canonical, status, timeout in _CODEX_PLAN:
            entry = {
                "type": "command",
                "command": stamp_command(f'sh "{script}" {canonical}',
                                         COMPONENT, __version__),
                "timeout": timeout,
            }
            if status:
                entry["statusMessage"] = status
            section.setdefault(event, []).append({"hooks": [entry]})
        landed["doc"] = doc
        return doc

    made = make_dir(host, host.codex_hooks.parent)
    if not host.codex_hooks.exists():
        made.append(host.codex_hooks)
    outcome = edit_json(host.codex_hooks, detection=detection, mutate=mutate,
                        strip=_remainder(roots))
    _record_created(host, made)
    count = _record_slots(host, "codex", host.codex_hooks, landed["doc"], script,
                          outcome)
    return [f"codex wired          {host.codex_hooks} ({count} stanzas; review with /hooks)"]


def codex_unwire(host: Host, roots: tuple[Path, ...] | None) -> list[str]:
    detection = host.detect("codex")
    if not detection.present or not host.codex_hooks.exists():
        return []
    _validate_codex(host)
    outcome = edit_json(host.codex_hooks, detection=detection,
                        mutate=lambda doc: strip_ours(doc, roots),
                        strip=_remainder(roots))
    _unrecord_slots(host, "codex", host.codex_hooks, outcome)
    return ["codex unwired        removed all NxtHooks-managed stanzas"] if outcome.changed else []


# ------------------------------------------------------- artifact installs


def tree_digest(root: Path) -> str:
    """A stable digest of a directory NxtOS owns outright.

    The marker file is excluded because it is written from this value: a
    digest that covered it could not be computed before it existed.
    """
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == MARKER_FILE_NAME:
            continue
        parts.append(str(path.relative_to(root)))
        parts.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return digest("\n".join(parts))


def _record_owned(host: Host, harness: str, file: Path, slot: Path, form: str,
                  marker, own_sha256: str, runtime: Path) -> None:
    ledger_append(
        Record(
            op=WIRE, component=COMPONENT, version=__version__, harness=harness,
            file=str(file), slot=str(slot), marker=str(marker), form=form,
            own_sha256=own_sha256, runtime=str(runtime),
        ),
        home=host.home, env=host.env,
    )


def _hook_sources_inside(lib: Path) -> list[Path]:
    """Live manifest sources that physically live under `lib`.

    Nothing should put a hook source inside the directory install replaces,
    but `nlm-session-start.py` did, as its only copy, and the rmtree below
    deleted it on the next install with no warning and no backup. Whether
    that placement was wise is a separate argument from whether we are
    entitled to destroy it.
    """
    found: list[Path] = []
    for entry in load_manifests():
        raw = entry.get("source") or ""
        if not raw:
            continue
        src = Path(os.path.expanduser(raw))
        if not src.is_file():
            continue
        try:
            src.resolve().relative_to(lib.resolve())
        except ValueError:
            continue
        found.append(src)
    return found


def _provision_builtin_hooks() -> list[str]:
    """Materialize the file(s) each builtin manifest's `source` points at.

    The shim writers live in nxthooks._vendor, and nxthooks may not import nxthooks._vendor --
    that import direction is what keeps this package shippable standalone. So
    this knows a writer only by the module name its own manifest declares, and
    runs it as a subprocess; a missing or failing writer is reported here,
    never swallowed, because a silent skip recreates the exact defect this
    exists to close: a curated builtin tier that GAPs on a cold machine
    because install never wrote what its own manifest points at.

    Driven by the manifests rather than by a hardcoded module list. It named
    `nxthooks._vendor.nxtlint.install` and nothing else, so adding `secret-scan` to
    the builtin tier would have wired a hook whose source nothing writes --
    a second copy of the same bug, added while fixing the first.
    """
    notes: list[str] = []
    for entry in builtin_manifests():
        module = entry.get("provision")
        if not module:
            notes.append(
                f"builtin hooks       FAILED: '{entry['name']}' declares no "
                "`provision` module; nothing will write its source"
            )
            continue
        # The writer must be imported out of the same tree this process is
        # running from, and neither default gets there: cwd-on-sys.path picks
        # up any directory that merely shares a name (running `install` from a
        # checkout of its own source is enough), and site-packages picks up
        # whatever was installed last, which on a dogfood machine is some
        # other worktree. So: `-P` to drop cwd, and PYTHONPATH pinned to this
        # package's parent, which is the importable root in both layouts --
        # `<repo>/` next to `nxthooks._vendor/`, and `<site-packages>/` for the
        # vendored `nxthooks._vendor`.
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_pkg_dir().parent)
        proc = subprocess.run(
            [_python(), "-P", "-m", str(module)],
            capture_output=True, text=True, check=False, timeout=30, env=env,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            reason = detail[-1] if detail else f"exited {proc.returncode}"
            notes.append(
                f"builtin hooks       FAILED to provision {entry['name']}: {reason}"
            )
            continue
        notes.append(f"builtin hooks       {proc.stdout.strip()}")
    return notes


# ---------------------------------------------------------------- commands


def install_runtime(host: Host, cfg: dict, source: Path | None = None) -> Path:
    root = host.root(cfg)
    lib = root / "lib" / "nxthooks"
    bin_dir = root / "bin"
    made = make_dir(host, bin_dir)
    made += make_dir(host, root / "lib")

    src_pkg = Path(source).resolve() if source else _pkg_dir()
    rescued: list[tuple[Path, bytes]] = []
    if lib.exists():
        rescued = [(p, p.read_bytes()) for p in _hook_sources_inside(lib)]
        shutil.rmtree(lib)
    shutil.copytree(src_pkg, lib, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for path, blob in rescued:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        path.chmod(0o755)

    # Dispatch imports the shared path and projection primitives. Ship them
    # with the runtime so a harness does not depend on the installer's
    # site-packages.
    #
    # Under `_framework/`, and appended to `sys.path` rather than prepended,
    # because what ships here is a *partial* `nxthooks._vendor`: paths and projection
    # only. `run_nxthooks.py` exposes the whole CLI, and `cmd_install` and
    # `cmd_uninstall` both import `nxthooks._vendor.plugins.delivery`. Sitting this
    # copy at `lib/nxthooks._vendor` shadowed a complete installation and turned
    # those two commands into `ModuleNotFoundError` on a machine where
    # nxthooks._vendor was fully present. A real one now wins whenever there is one,
    # and this remains the fallback it was meant to be.
    # A vendored build carries paths and projection inside the package, so the
    # `copytree` of `lib/nxthooks/` above has already shipped them and there is
    # no partial copy to place or shadow. `io-whtnxt/nxthooks` is that build;
    # this repo is not, and both must work from one source.
    framework: Path | None = None
    if not (src_pkg / "_vendor").is_dir():
        import nxthooks._vendor.projection

        framework = root / "lib" / FRAMEWORK_DIR / "nxthooks/_vendor"
        if framework.exists():
            shutil.rmtree(framework)
        framework.mkdir(parents=True)
        shared = Path(paths.__file__).parent
        for name in ("__init__.py", "paths.py"):
            shutil.copy2(shared / name, framework / name)
        shutil.copytree(Path(nxthooks._vendor.projection.__file__).parent, framework / "projection",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    shutil.copy2(src_pkg / "adapters" / "run_nxthooks.py", root / "lib" / "run_nxthooks.py")

    for name in ("cc-dispatch.sh", "codex-dispatch.sh"):
        body = (src_pkg / "adapters" / name).read_text(encoding="utf-8")
        target = bin_dir / name
        target.write_text(
            body.replace("{{PYTHON}}", _python())
                .replace("{{ROOT}}", str(root))
                .replace("{{STATE}}", str(host.state_dir)),
            encoding="utf-8",
        )
        target.chmod(0o755)

    (root / "VERSION").write_text(__version__ + "\n", encoding="utf-8")
    # The inventory uninstall removes from, written by the install that put
    # each file there. Without it uninstall was an `rmtree` of the whole root,
    # which took anything the operator had put beside the runtime with it.
    owned = [str((lib / path.relative_to(src_pkg)).relative_to(root))
             for path in src_pkg.rglob("*")
             if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    # `framework` exists only on a non-vendored build; on a vendored one the
    # `src_pkg` walk above already listed those files, because they live
    # inside the package.
    if framework is not None:
        owned += [str(path.relative_to(root)) for path in framework.rglob("*") if path.is_file()]
    owned += ["lib/run_nxthooks.py", "bin/cc-dispatch.sh", "bin/codex-dispatch.sh", "VERSION"]
    (root / RUNTIME_INVENTORY).write_text(
        json.dumps({"version": 1, "files": sorted(owned)}), encoding="utf-8")
    # The ledger's own directory. `ledger.append` builds it with `mkdir
    # (parents=True)` and nothing would then know NxtOS had, which is how a
    # `~/.local/state` outlives the uninstall that created it.
    made += make_dir(host, paths.state_home(host.env, host.home))
    _record_created(host, made)
    return root


def _install_owned_tree(host: Host, harness: str, source: Path, dest: Path,
                        runtime: Path) -> tuple[Path, bool]:
    """Copy a directory NxtOS owns outright and stamp it `.nxt-managed`.

    Detect-gated like every other write. `copytree` builds missing parents,
    which is how a machine with no pi came to have a `~/.pi`: the directory
    was a side effect of the copy, never a decision.
    """
    if not host.detect(harness).present:
        return dest, False
    made = missing_ancestors(dest, host.home)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dest / "VERSION").write_text(__version__ + "\n", encoding="utf-8")
    source_sha = tree_digest(dest)
    marker = build(COMPONENT, __version__, source_sha)
    (dest / MARKER_FILE_NAME).write_text(
        marker_file_body(marker, source_sha, str(runtime)), encoding="utf-8"
    )
    _record_created(host, made)
    _record_owned(host, harness, dest / MARKER_FILE_NAME, dest, FORM_MARKER_FILE,
                  marker, source_sha, runtime)
    return dest, True


def install_pi_transport(host: Host, root: Path) -> tuple[Path, bool]:
    return _install_owned_tree(
        host, "pi", _pkg_dir() / "adapters" / "pi-extension", host.pi_extension,
        root / "lib" / "run_nxthooks.py",
    )


def install_hermes_transport(host: Host, root: Path) -> tuple[Path, bool]:
    """Copy the plugin into Hermes's user plugin dir.

    `~/.hermes/plugins/` rather than the bundled plugin dir inside the
    hermes-agent checkout: the user dir survives a `hermes update`, which
    replaces the repo. Installing does not activate anything -- Hermes plugins
    are opt-in per profile via `plugins.enabled`.
    """
    return _install_owned_tree(
        host, "hermes", _pkg_dir() / "adapters" / "hermes-plugin", host.hermes_plugin,
        root / "lib" / "run_nxthooks.py",
    )


def install_opencode_transport(host: Host, root: Path) -> tuple[Path, bool]:
    dest = host.oc_plugin
    if not host.detect("opencode").present:
        return dest, False
    body = (_pkg_dir() / "adapters" / "opencode-plugin" / "nxthooks.ts").read_text(
        encoding="utf-8"
    )
    runtime = root / "lib" / "run_nxthooks.py"
    marker = build(COMPONENT, __version__, body)
    made = make_dir(host, dest.parent)
    dest.write_text(f"// {marker}\n{body}", encoding="utf-8")
    _record_created(host, made)
    _record_owned(host, "opencode", dest, dest, FORM_COMMENT_LINE, marker,
                  digest(body), runtime)
    return dest, True


# ------------------------------------------------------------------ seeds


SEED_CONFIG = """\
# NxtHooks canonical config — edited by hand or by the NxtOS UI; both are
# first-class. Schema: the NxtOS component-config spec

[meta]
version = 1

[install]
root = "~/.local/share/nxt/hooks"

[policy]
default = "enforce"          # per-hook overrides live in hooks.d/*.toml

[scope]
include = []                 # empty = all sessions
exclude = []                 # e.g. ["**/.worktrees/**"]

# Written by `nxthooks install` from what it detected on this machine. This
# block is a record, not an instruction: install wires what is present, and
# editing a harness to true here does not make NxtHooks manufacture it.
[harnesses]
claude-code = false
codex = false
pi = false
opencode = false
hermes = false
"""


#: Empty, and that is the point (uniformity plan N2-c).
#:
#: This dict used to carry seven manifests whose `source` pointed at
#: `~/.nxtos/bin/*` -- scripts from the operator's own machine that no wheel
#: installs. On a stranger's machine all seven resolved to nothing, `verify`
#: reported seven gaps the printed advice could not repair, and `doctor` said
#: the guards had run. A component may seed a hook whose implementation it
#: ships; it may not seed one it does not. NxtGit's five manifests go with
#: NxtGit's own `install`, and `secret-scan` with nxtgrep's.
#:
#: The operator's live `hooks.d/` is untouched: seeding never overwrote an
#: existing manifest, and nothing here deletes one.
PILOT_MANIFESTS: dict[str, str] = {}


def seed_config(host: Host, force: bool = False) -> bool:
    cfg_path = host.config_path
    if cfg_path.exists() and not force:
        return False
    made = make_dir(host, cfg_path.parent)
    cfg_path.write_text(SEED_CONFIG, encoding="utf-8")
    _record_created(host, made)
    return True


def seed_manifests(host: Host) -> list[str]:
    """Write any manifest missing from the store.

    An existing manifest is never overwritten: local edits to policy or
    matchers are the operator's, not ours to reset.
    """
    seeded = []
    made = make_dir(host, host.hooks_d)
    _record_created(host, made)
    for name, body in PILOT_MANIFESTS.items():
        target = host.hooks_d / name
        if target.exists():
            continue
        target.write_text(body, encoding="utf-8")
        seeded.append(name)
    return seeded


# ---------------------------------------------------------------- commands


def declare_harnesses(host: Host, selected: list[str]) -> None:
    """Record in config which harnesses this install actually wired.

    A record of a reading, not a switch. `cmd_install` and `cmd_verify` both
    ask `detect`; this is what lets an operator see the answer without
    re-running either.
    """
    path = host.config_path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    chosen = set(selected)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in HARNESSES and "=" in line:
            out.append(f"{key} = {'true' if key in chosen else 'false'}")
        else:
            out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def present_harnesses(host: Host) -> tuple[list[str], dict[str, Detection]]:
    """What the machine actually has, ignoring any `--harnesses` assertion.

    `Host.detect` answers with the operator's assertion folded in, which is
    what the writers need. This answers the question the DoD asks, which is a
    different one.
    """
    detections = {name: detect(name, home=host.home, env=host.env,
                               path_lookup=host.path_lookup)
                  for name in HARNESSES}
    return [name for name, d in detections.items() if d.present], detections


@exclusive_lifecycle
def cmd_install(args, host: Host | None = None) -> int:
    """`host` is the machine to install onto, and defaults to this one.

    A parameter rather than `Path.home()` inside, because a test suite that
    can only exercise this against the developer's real `~/.claude` is a test
    suite that will not exercise it.
    """
    host = host or Host.local()
    if args.harnesses:
        host = replace(host, forced=frozenset(args.harnesses))

    # Seed before anything reads config. The other order is what made a
    # stranger's first install end exit 1: install wired the built-in
    # defaults and verify then judged it against the freshly written seed.
    if seed_config(host):
        print(f"config seeded       {host.config_path}")
    cfg = host.config()

    present, detections = present_harnesses(host)
    explicit = list(args.harnesses or [])
    harnesses = explicit or present

    for name in HARNESSES:
        mark = "present" if detections[name].present else "absent "
        print(f"detected  {mark}   {name:12} {detections[name].reason}")
    for name in explicit:
        if name not in present:
            print(f"WARNING   {name} was named explicitly but was not detected; "
                  f"wiring it will create {detections[name].root}")

    for line in _provision_builtin_hooks():
        print(line)

    source = Path(args.source).resolve() if getattr(args, "source", None) else _pkg_dir()
    print(f"source              {_source_provenance(source)}")
    root = install_runtime(host, cfg, source=source)
    print(f"runtime installed   {root}")

    declare_harnesses(host, harnesses)
    for name in seed_manifests(host):
        print(f"manifest seeded     hooks.d/{name}")

    changes: list[str] = []
    if "claude-code" in harnesses:
        changes += cc_wire(host, root, wiring_groups(host))
    if "codex" in harnesses:
        changes += codex_wire(host, root)
    if "pi" in harnesses:
        dest, ok = install_pi_transport(host, root)
        if ok:
            changes.append(f"pi transport        {dest}")
    if "opencode" in harnesses:
        dest, ok = install_opencode_transport(host, root)
        if ok:
            changes.append(f"opencode transport  {dest}")
    if "hermes" in harnesses:
        dest, ok = install_hermes_transport(host, root)
        if ok:
            changes.append(f"hermes transport    {dest} (enable per profile: plugins.enabled)")

    for line in changes:
        print(line)

    # The pane, last, because it should follow the wiring rather than promise
    # it. It lives in this package and arrives through this verb so that a
    # machine without NxtHooks has no NxtHooks pane (N4-c-1); until now the
    # manifest sat in the host package and the pane appeared whether or not
    # this command had ever run.
    # Imported here, not at module scope. `install_runtime` copies this whole
    # package to `<root>/lib` and the harness transports run it from there
    # through `run_nxthooks.py`, which imports `cli` and therefore this module
    # on *every* hook dispatch. `nxthooks._vendor.plugins.delivery` pulls in the
    # manifest loader and jsonschema behind it, and a dispatch running under a
    # sandboxed HOME loses the user site-packages that provide it -- measured,
    # not hypothetical: every synthetic dispatch in `nxthooks doctor` died with
    # ModuleNotFoundError the moment this sat at the top of the file. Install
    # is the one path that needs it and the one path that can afford it.
    from nxthooks._vendor.plugins.delivery import deliver

    written, problem = deliver(PLUGIN_MANIFEST, host.home)
    print(f"pane not installed  {problem}" if problem
          else f"pane installed      {written}")

    print("\nverify:")
    return cmd_verify(args, host)


def _remove_owned_paths(host: Host) -> list[str]:
    """Every directory and file NxtOS owns outright, ledger first.

    The ledger is authoritative, so a transport whose marker a human deleted
    is still removed. The three well-known destinations are swept afterwards
    for a machine whose ledger predates it or was lost.
    """
    lines = []
    targets: list[Path] = []
    for record in live_slots(COMPONENT, home=host.home, env=host.env):
        if record.form not in (FORM_MARKER_FILE, FORM_COMMENT_LINE):
            continue
        targets.append(Path(record.slot))
        # Closing the slot, not just deleting the file. A removal the ledger
        # does not hear about leaves the slot live forever, which makes every
        # later `live_slots` read report a projection that is not there --
        # and keeps the family ledger alive after the last thing in it went.
        ledger_append(
            Record(
                op=UNWIRE, component=COMPONENT, version=__version__,
                harness=record.harness, file=record.file, slot=record.slot,
                marker=record.marker, form=record.form,
                own_sha256=record.own_sha256, runtime=record.runtime,
            ),
            home=host.home, env=host.env,
        )
    targets += [host.pi_extension, host.oc_plugin, host.hermes_plugin]
    for path in dict.fromkeys(targets):
        if path.is_dir():
            shutil.rmtree(path)
            lines.append(f"removed             {path}")
        elif path.exists():
            path.unlink()
            lines.append(f"removed             {path}")
    return lines


def _retire_ledger(host: Host) -> list[str]:
    """Remove the family ledger once nothing is projected anywhere.

    It is family state, not this component's, so it survives while any other
    component still has a live slot in it. When none does, an append-only log
    whose folded state is empty is residue on a stranger's disk, and the
    uninstall that created it is the one that has to take it away.
    """
    ledger = paths.state_home(host.env, host.home) / "projections.jsonl"
    if not ledger.exists():
        return []
    if live_slots(home=host.home, env=host.env):
        return [f"ledger preserved    {ledger} (other components still projected)"]
    ledger.unlink()
    lock = ledger.with_name(ledger.name + ".nxt.lock")
    if lock.exists():
        lock.unlink()
    return [f"removed             {ledger} (no projections remain)"]


def _remove_locks(host: Host, files) -> None:
    """The `<file>.nxt.lock` sidecars `projection.write` leaves next to every
    file it edits. They are ours, they are empty, and a stranger's `.claude/`
    keeping one after uninstall is residue like any other."""
    for file in files:
        lock = file.with_name(file.name + ".nxt.lock")
        if lock.exists():
            lock.unlink()


#: The four files the runtime owns outside its two package directories.
_RUNTIME_TOPLEVEL = ("lib/run_nxthooks.py", "bin/cc-dispatch.sh",
                     "bin/codex-dispatch.sh", "VERSION")


def _remove_runtime(root: Path, known: bool) -> bool:
    """Remove the files this install put here, and report whether it did.

    Returns False having removed nothing when ownership cannot be
    established. The caller must not announce a cleanup it did not get: the
    two prints used to run back to back, so an operator with no inventory and
    an unknown root was told the runtime was preserved and that cleanup had
    completed, in consecutive lines.
    """
    marker = root / RUNTIME_INVENTORY
    if marker.is_file() and not marker.is_symlink():
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
        except ValueError as exc:
            # The inventory is the thing that survives a crash mid-install, so
            # a corrupt one is the case it most has to answer for. Reading it
            # carefully for shape and then letting a decode error out as a
            # traceback answered the easy half only.
            raise ValueError(
                f"Unreadable native runtime inventory; refusing cleanup: {exc}") from None
        if record.get("version") != 1 or not isinstance(record.get("files"), list):
            raise ValueError("Invalid native runtime inventory; refusing cleanup")
        relatives = record["files"]
    elif known:
        package = _pkg_dir()
        relatives = ["lib/nxthooks/" + str(p.relative_to(package))
                     for p in package.rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts]
        relatives += list(_RUNTIME_TOPLEVEL)
    else:
        print("runtime preserved: native ownership could not be established")
        return False

    candidates = []
    for relative in relatives:
        if not isinstance(relative, str) or Path(relative).is_absolute() \
                or ".." in Path(relative).parts:
            raise ValueError("Invalid native runtime inventory path")
        if relative not in _RUNTIME_TOPLEVEL and not relative.startswith(
                ("lib/nxthooks/", f"lib/{FRAMEWORK_DIR}/nxthooks._vendor/")):
            raise ValueError("Unexpected native runtime inventory path")
        path = root / relative
        if not path.parent.resolve().is_relative_to(root.resolve()):
            raise ValueError("Native runtime path crosses a symlink")
        candidates.append(path)

    for source in list(candidates):
        cache = source.parent / "__pycache__"
        if source.suffix == ".py" and cache.is_dir() and not cache.is_symlink():
            candidates.extend(cache.glob(source.stem + ".*.pyc"))

    directories = {parent for path in candidates for parent in path.parents
                   if parent != root and parent.is_relative_to(root)}
    for path in candidates:
        if path.is_file() or path.is_symlink():
            path.unlink()
    if marker.exists():
        marker.unlink()
    # Deepest first, and `rmdir` only, so a directory still holding something
    # the operator put there survives with its contents.
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            try:
                path.rmdir()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass
    return True


@exclusive_lifecycle
def cmd_uninstall(args, host: Host | None = None) -> int:
    host = host or Host.local()
    cfg = host.config()
    root = host.root(cfg)
    created = _read_created(host)
    roots = _known_roots(host)
    scoped = tuple({*(roots or ()), root})

    from nxthooks._vendor.plugins.delivery import withdraw  # see cmd_install

    removed, problem = withdraw(PLUGIN_ID, host.home)
    if problem:
        print(f"ERROR               {problem}")
    elif removed is not None:
        print(f"removed             {removed}")

    for line in cc_unwire(host, scoped):
        print(line)
    for line in codex_unwire(host, scoped):
        print(line)
    if roots is None:
        # No ledger and no legacy record: this machine predates both, so the
        # configured root may already have drifted from whatever wired the
        # live stanzas. Strip by marker alone rather than leave one wired
        # against a script the rmtree below is about to delete.
        for line in cc_unwire(host, None):
            print(line)
        for line in codex_unwire(host, None):
            print(line)

    for line in _remove_owned_paths(host):
        print(line)

    if not args.keep_runtime and root.exists():
        if _remove_runtime(root, bool(roots and root in roots)):
            print(f"native runtime cleanup complete: {root}")

    for line in _retire_ledger(host):
        print(line)
    _remove_locks(host, (host.cc_settings, host.codex_hooks))

    legacy_record = host.config_dir / "installed-roots"
    if legacy_record.exists():
        legacy_record.unlink()

    if args.purge_config and host.config_dir.exists():
        for owned in (host.config_path,host.config_dir/CREATED_PATHS):
            if owned.is_file() or owned.is_symlink():owned.unlink()
        for directory in (host.hooks_d,host.config_dir):
            try:directory.rmdir()
            except OSError:pass
        print('native configuration removed; hook sources and unrelated files preserved')
    else:
        print(f"config preserved    {host.config_dir} (--purge-config to remove)")

    if host.state_dir.exists():
        try:host.state_dir.rmdir()
        except OSError:pass

    for path in prune_created(host, created):
        print(f"removed             {path} (empty; NxtOS created it)")
    # `created` was read out of the catalog, and `--purge-config` deleted the
    # catalog above -- so on a second uninstall, or on the one the host
    # cascades into, that record is already gone and the directories it named
    # survive. The pane also lives under the *host's* plugin root, which this
    # record never covered at all. The shared sweep takes whatever is left
    # empty under `nxt/`, which is the only rule either case needs.
    for path in paths.prune_empty_roots(host.env, host.home):
        print(f"removed             {path} (empty)")

    print("unwired. existing unmanaged hooks were never touched.")
    return 0


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "ok  " if ok else "GAP "
    suffix = f"  {detail}" if detail else ""
    print(f"  [{mark}] {label}{suffix}")
    return ok


CAPABILITY_MATRIX = """\
capability matrix (effective enforcement):

  hook class              claude-code  codex       pi            opencode              hermes
  ---------------------   -----------  ----------  -----------   -------------------   ----------
  session_start inject    inject       inject      inject        (no event; gap)       inject
  prompt_submit inject    inject       inject      inject        inject (advise-only)  inject
  before_tool block       block        block*      block** /     block (throw)         block
                                                    preflight***
  after_tool observe      observe      observe     observe       observe               observe
  stop/session_close      not adapted  wired****   not adapted   not adapted           not adapted

  * Codex PreToolUse covers Bash, apply_patch, MCP, and most local function
    tools. Hosted and specialized tool paths can bypass it, so it is a strong
    guardrail rather than a complete security boundary.
  ** pi blocks for real, through `tool_call` ("Fired before a tool executes.
    Can block."), whose handler returns {block, reason}. This row read "pi
    has no pre-tool blocking point" until 2026-09-15 -- asserted, never
    checked, and it cost pi every blocking hook in the catalogue. Verified
    live against a refusing hook.
  *** A pi_preflight hook has a block condition knowable without tool/args;
    pi dispatches it at turn start and turns a block verdict into an
    interactive stay-or-switch-style confirmation. Unchanged by the above:
    it answers before any tool has been chosen, which the real gate cannot.
  **** Codex maps stop to Stop and session_close to SessionEnd. SessionEnd is
    advisory and can be delayed until the thread is actually closed/idle.

  Every harness above has a real pre-tool gate as of 2026-09-15; pi was the
  last without one and only appeared to be. What remains is Codex's footnote:
  PreToolUse covers Bash, apply_patch, MCP and most local function tools, and
  hosted or specialised tool paths can route around it. Treat before_tool as
  a strong guardrail there rather than a complete boundary, and put anything
  that must not be bypassed behind after_tool as well, which observes what
  actually ran. Instructions in an AGENTS.md remain advice to a model, never
  enforcement.

  hermes maps onto its own vocabulary, not matching names:
    session_start  a registered system prompt section, re-evaluated per new
                   session. NOT on_session_start, whose return is discarded --
                   injecting there would be a silent no-op.
    prompt_submit  pre_llm_call. Context merges into the current turn's user
                   message. It cannot veto the call, so a block verdict
                   degrades to an in-context warning, as on pi.
    before_tool    pre_tool_call, a real pre-execution veto. Hermes also
                   offers an approve directive escalating to its human gate,
                   which no other harness has; unused for now.
    after_tool     post_tool_call, return discarded.
  Its transport is Python loaded into the agent process, not a subprocess
  invoked with JSON on stdin, and plugins are opt-in per profile: installing
  activates nothing until the profile's config.yaml lists it in
  plugins.enabled."""


def _report_projections(host: Host) -> bool:
    """Tri-state per ledger slot: intact, altered, missing.

    Presence alone was the old check, and two states is where the drift hid:
    a stanza a human edited and a stanza NxtOS wrote read identically.
    """
    report = verify_slots(COMPONENT, home=host.home, env=host.env)
    if not report.slots:
        print("  [ok  ] projections            none recorded")
        return not report.operational
    for slot in report.slots:
        mark = {INTACT: "ok  ", ALTERED: "GAP ", MISSING: "GAP "}[slot.state]
        detail = slot.detail or f"{slot.file.name}{slot.slot}"
        print(f"  [{mark}] {slot.harness} {slot.state:8} {detail}")
        if not slot.runtime_present:
            print(f"  [GAP ] {slot.harness} runtime gone      {slot.runtime}")
    for file_name, moved in sorted(report.foreign_changed.items()):
        if moved:
            print(f"  [note] {file_name} was rewritten by its harness since "
                  "NxtOS last wrote it")
    for warning in report.operational:
        print(f"  [GAP ] ledger unreadable      {warning}")
    return report.exit_code == 0


def cmd_verify(args, host: Host | None = None) -> int:
    host = host or Host.local()
    cfg = host.config()
    root = host.root(cfg)
    ok = True
    print(f"nxthooks verify v{__version__}")
    ok &= _check("config file", host.config_path.exists(), str(host.config_path))
    ok &= _check("dispatcher runtime", (root / "lib" / "run_nxthooks.py").is_file(),
                 str(root))
    ok &= _check("cc transport", (root / "bin" / "cc-dispatch.sh").is_file())

    from .registry import resolve

    entries, collisions = resolve(hooks_d=host.hooks_d)
    ok &= _check("manifest directory", host.hooks_d.is_dir(), f"{len(entries)} hook(s)")
    pending = [e for e in entries if _source_state(e) == "pending"]
    for entry in entries:
        ok &= _check_source(entry)
    # registry.render_collisions, not a second copy of its format string: the
    # first version of this line was a duplicate, so improving the warning in
    # registry.py changed nothing an operator ever saw.
    render_collisions(collisions)

    _, detections = present_harnesses(host)
    #: A harness install wired on an explicit `--harnesses` is checked even
    #: though it is not detected: the ledger is the record of what was
    #: actually written, and a slot nothing verifies is a slot that can rot.
    wired = {record.harness
             for record in live_slots(COMPONENT, home=host.home, env=host.env)}

    def harness_check(name, label, passed, detail=""):
        """Check the harnesses this machine has, plus any this install wired.

        Detected, never declared. Reading the answer out of `[harnesses]` in
        the config is what let install and verify contradict each other on
        every fresh machine, and a report with standing gaps nobody can close
        trains its reader to stop reading it.
        """
        if not detections[name].present and name not in wired:
            print(f"  [n/a ] {label}  {detections[name].reason}")
            return True
        return _check(label, passed, detail)

    wired_events = _wired_events(host.cc_settings, EVENT_TO_CC)
    #: Only the events some live hook declares. The list used to be the three
    #: hardcoded here, which made verify disagree with install in both
    #: directions: it reported a GAP for an event install correctly declined to
    #: wire because nothing needed it, and it never checked after_tool at all,
    #: which is the one event the only builtin declares.
    expected = wiring_groups(host)
    for canonical in EVENT_TO_CC:
        label = f"claude-code wiring ({canonical})"
        if not expected.get(canonical):
            # Said, not skipped. An event absent from the report reads as an
            # event nobody checked; this is a deliberate non-wiring and the
            # reader is entitled to see the reason.
            print(f"  [n/a ] {label}  no live hook declares this event")
            continue
        ok &= harness_check("claude-code", label, canonical in wired_events)

    codex_valid, codex_error = True, ""
    try:
        _validate_codex(host)
    except CodexHooksError as exc:
        codex_valid, codex_error = False, str(exc)
    ok &= harness_check("codex", "codex hooks JSON", codex_valid, codex_error)
    codex_events = _wired_events(host.codex_hooks, EVENT_TO_CODEX)
    for canonical in EVENT_TO_CODEX:
        ok &= harness_check("codex", f"codex wiring ({canonical})",
                            canonical in codex_events)

    ok &= harness_check("pi", "pi transport",
                        (host.pi_extension / "index.ts").is_file(),
                        str(host.pi_extension))
    ok &= harness_check("opencode", "opencode transport", host.oc_plugin.is_file(),
                        str(host.oc_plugin))
    ok &= harness_check("hermes", "hermes transport",
                        (host.hermes_plugin / "__init__.py").is_file(),
                        str(host.hermes_plugin))
    ok &= harness_check("codex", "codex transport",
                        (root / "bin" / "codex-dispatch.sh").is_file())

    ok &= _report_projections(host)
    _report_overlaps(host, entries)

    print()
    print(CAPABILITY_MATRIX)
    if not ok:
        print("\nresult: GAPS FOUND — run `python3 -m nxthooks install` to repair.")
        return 1
    # Exit code unchanged: neither a pending source nor a shadowed user hook is
    # a gap. The sentence has to count them anyway -- "all wiring resolves"
    # printed above N [pend] and M [warn] lines contradicts the lines above it,
    # and the reader believes the summary.
    notes = []
    if pending:
        providers = ", ".join(sorted({e["provider"] for e in pending}))
        notes.append(f"{len(pending)} source(s) pending from {providers}")
    if collisions:
        notes.append(f"{len(collisions)} user hook(s) shadowed")
    detail = f" ({', '.join(notes)})" if notes else ""
    print(f"\nresult: all wiring resolves{detail}.")
    return 0


def _wired_events(path: Path, vocabulary: dict) -> set[str]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return set()
    found = set()
    for _pointer, command in walk_commands(doc):
        if not owned_command(command, None):
            continue
        for canonical in vocabulary:
            if canonical in command:
                found.add(canonical)
    return found


def _report_overlaps(host: Host, entries: list[dict]) -> None:
    """A hook source the directory manages that is ALSO wired directly by a
    legacy stanza (pre-NxtHooks wiring left in place)."""
    import re

    try:
        doc = json.loads(host.cc_settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        doc = {}
    managed_sources = {Path(os.path.expanduser(e["source"])).name for e in entries}
    foreign_scripts = set()
    for _pointer, command in walk_commands(doc):
        if owned_command(command, None):
            continue
        for match in re.finditer(r"([A-Za-z0-9_-]+\.(?:sh|py))", command):
            foreign_scripts.add(match.group(1))
    overlaps = sorted(managed_sources & foreign_scripts)
    if overlaps:
        print("\noverlap notice: these scripts fire via BOTH NxtHooks and legacy")
        print("direct stanzas (harmless — guards are idempotent — but redundant):")
        for name in overlaps:
            print(f"  - {name}")
        print("  retire the legacy stanzas once you trust the NxtHooks path.")
    else:
        print("\nno managed/legacy overlaps detected in ~/.claude/settings.json")
        print("(workspace-level .claude/settings.json files are not scanned in v1)")


@exclusive_lifecycle
def cmd_doctor(args, host: Host | None = None) -> int:
    host = host or Host.local()
    code = cmd_verify(args, host)
    print("\ndispatch self-test (synthetic envelopes):")
    runner = host.root() / "lib" / "run_nxthooks.py"
    cases = [
        ("before_tool", {"cwd": os.getcwd(), "tool_name": "Bash",
                         "tool_input": {"command": "echo hi"}}),
        ("prompt_submit", {"cwd": os.getcwd(), "prompt": "hello"}),
    ]
    # The dispatcher is a subprocess, so it resolves its own hook directory
    # from its own environment. Handing it this host's is what makes the
    # self-test a statement about the machine being installed onto rather
    # than about whichever `$HOME` the interpreter happened to inherit.
    child_env = {**os.environ, **host.env, "HOME": str(host.home)}
    for event, payload in cases:
        proc = subprocess.run(
            [_python(), str(runner), "dispatch", "--harness", "claude-code",
             "--event", event, "--out", "json"],
            input=json.dumps(payload), capture_output=True, text=True, timeout=30,
            env=child_env,
        )
        if proc.returncode != 0:
            print(f"  [GAP ] {event}: dispatcher exited {proc.returncode}: "
                  f"{proc.stderr[:200]}")
            code = code or 1
            continue
        outcome = json.loads(proc.stdout)
        ran = ", ".join(r["hook"] for r in outcome["ran"]) or "(no hooks matched)"
        verdict = outcome["verdict"] + (f": {outcome['reason'][:80]}"
                                        if outcome["reason"] else "")
        print(f"  [ok  ] {event} -> {verdict}  [{ran}]")
        # A hook whose source does not exist did not run, and reporting it in
        # the same breath as one that did is how a stranger came to be told
        # two guards were active that could not have executed.
        for missing in outcome.get("unavailable") or []:
            print(f"  [GAP ] {event}: {missing['hook']} did not run — "
                  f"{missing['error']}")
            code = code or 1
    return code


def cmd_list(args, host: Host | None = None) -> int:
    host = host or Host.local()
    entries = load_manifests(host.hooks_d)
    if not entries:
        print("no hooks in the directory.", host.hooks_d)
        return 0
    # TIER is the only place shadowing is visible: a user manifest wins over
    # its builtin counterpart by design, so a machine seeded by an older
    # install shows `user` on hooks it never knowingly overrode.
    print(f"{'NAME':24} {'TIER':8} {'EVENTS':32} {'MATCHERS':28} POLICY   SOURCE")
    for entry in entries:
        events = ",".join(entry["events"])
        matchers = ",".join(entry.get("matchers") or ["*"])
        policy = entry.get("policy", "enforce")
        tier = entry.get("tier", "user")
        print(f"{entry['name']:24} {tier:8} {events:32} {matchers:28} {policy:8} {entry['source']}")
    return 0
