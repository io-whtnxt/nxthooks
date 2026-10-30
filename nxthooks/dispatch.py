"""The NxtHooks dispatcher.

One entrypoint every harness transport calls:

    python3 -m nxthooks dispatch --harness <name> --event <canonical-event>

Reads the harness envelope on stdin, normalizes it, selects matching hooks
from the directory, executes their sources against a harness-agnostic
contract, and emits either the harness-native protocol (--out native) or
the generic JSON verdict (--out json) for TS adapters.

Hook source contract (what every hook script is written against):

  stdin   normalized envelope JSON
  env     NXTHOOKS_HARNESS / _EVENT / _TOOL / _CWD
  exit 0  allow; stdout 'CTX: ...' lines become context injections
  exit 2  block; stderr is the reason shown to the model
  other   hook failure -> manifest fail mode (open=allow+note, closed=block)

Policy downgrades: an 'advise' hook's block becomes a context note and the
call proceeds. Capability honesty lives in the adapters, not here.

Order: enforce hooks run before advise hooks, and the first enforce veto ends
the dispatch (`run_hooks`). --deadline-ms is the transport's own budget: hooks
are cut to fit it, and a selection line ({"selected", "enforce"}) is printed
before the outcome so a transport that gives up still knows a guard was in play.

--preflight: pi has no pre-tool interception point, so a before_tool hook
normally can't run there at all. A before_tool hook marked pi_preflight in
its manifest is exempt from that -- its block condition is knowable with no
tool/args, so pi dispatches it with --preflight (tool="") at the start of
its turn instead of skipping it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import CANONICAL_EVENTS, HARNESSES, __version__
from .config import load_config, scope_allows
from .manifest import harness_enabled, load_manifests, matcher_matches, source_path
from .platform import state_dir

DEFAULT_TIMEOUT_MS = 5000
DEFAULT_CONTEXT_MAX_CHARS = 4000

#: Where the record of hook failures lives, and how often it is repeated to a
#: human. See `record_failures`.
FAILURE_LEDGER = "hook-failures.json"
ANNOUNCE_SECONDS = 3600
#: How far back `verify` speaks up about the ledger. The ledger itself is
#: kept history, unbounded by the same window: see `_report_hook_failures`.
FAILURE_REPORT_WINDOW = 7 * 86400


def failure_ledger_path() -> Path:
    return state_dir() / FAILURE_LEDGER


def read_failure_ledger() -> dict:
    """Per-hook failure counts. {} when there is no ledger or it is unreadable:
    the ledger is the messenger, and a damaged messenger must not break a call."""
    try:
        data = json.loads(failure_ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_failure_ledger(data: dict) -> None:
    path = failure_ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return


def failing(results: list[dict]) -> list[dict]:
    """Results where the hook did not do its job: it errored, timed out, could
    not be exec'd, or its source was not on this machine at all."""
    return [res for res in results if res.get("error")]


def _announce(res: dict, entry: dict, now: float) -> str:
    first = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("first_at", now)))
    verdict = "blocked this call" if res["verdict"] == "block" else "the call proceeded without it"
    return (
        f"[nxthooks] '{res['hook']}' has failed {entry['count']} time"
        f"{'s' if entry['count'] != 1 else ''} since {first}; "
        f"last error: {entry['last_error']}. "
        f"policy={entry.get('policy')} fail={entry.get('fail')}, so {verdict}. "
        "A hook that fails on every run is not enforcing anything, however quiet that is. "
        "Fix it, or say so out loud: python3 -m nxthooks doctor"
    )


def record_failures(results: list[dict], *, now: float | None = None) -> list[str]:
    """Keep a persistent, per-hook count of failures and announce each one at
    most once an hour.

    Why this exists. A hook that fails is turned into an allow plus a context
    note by its `fail = "open"` manifest, and that note is the only trace of
    the failure -- so it is dropped whenever a different hook blocks the call
    (the block reason is all native Claude Code protocol carries on a veto),
    and on an allow it goes out over a channel the harness may not show
    anyone. `doctor` checked that the hook's *file* existed. A fleet-wide
    snapshot safety net died on 2026-10-07 under a global `core.hooksPath`
    gate and every one of these layers reported green for it, which is the
    2026-08-20 lesson re-learned: silence is not evidence.

    So the failure is written down where it outlives the call, and repeated at
    a human on the channel the model actually reads, throttled per hook so the
    hundredth failure of the hour is not the one that trains everyone to
    ignore it. The ledger's timestamps are epoch seconds; `now` is injectable
    for tests.
    """
    problems = failing(results)
    clean = [res["hook"] for res in results if res.get("ran") and not res.get("error")]
    if not problems and not clean:
        return []
    now = time.time() if now is None else now
    ledger = read_failure_ledger()
    # A hook that runs clean after failing is the only proof it recovered;
    # verify reads last_ok_at to stop reporting it. One write per recovery.
    recovered = False
    for name in clean:
        entry = ledger.get(name)
        if isinstance(entry, dict) and float(entry.get("last_ok_at", 0)) < float(entry.get("last_at", 0)):
            entry["last_ok_at"] = now
            recovered = True
    if not problems:
        if recovered:
            _write_failure_ledger(ledger)
        return []
    alerts: list[str] = []
    for res in problems:
        name = res["hook"]
        entry = ledger.get(name) if isinstance(ledger.get(name), dict) else {}
        entry.setdefault("first_at", now)
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_at"] = now
        entry["last_error"] = (res["error"] or "")[:300]
        entry["policy"] = res.get("policy", "")
        entry["fail"] = res.get("fail", "")
        entry["ran"] = bool(res.get("ran", True))
        entry["last_verdict"] = res.get("verdict", "")
        if now - float(entry.get("announced_at", 0)) >= ANNOUNCE_SECONDS:
            entry["announced_at"] = now
            alerts.append(_announce(res, entry, now))
        ledger[name] = entry
    _write_failure_ledger(ledger)
    return alerts


def note_failures(outcome: dict, alerts: list[str]) -> dict:
    """Attach the alerts to the outcome so every emission path carries them.

    `context` is recomputed through the same bounded-composition helper the
    aggregates use, so the char/token accounting still describes what ships.
    `alerts` is checked independently of the context budget: a dead guard is
    not optional context, and `emit_native_*` print it even on a veto, where
    the ordinary context of a blocked call is dropped by protocol.
    """
    outcome["alerts"] = list(alerts)
    if alerts:
        bounded = _bounded_context_text(alerts + list(outcome.get("context") or []))
        outcome["context"] = [bounded] if bounded else []
        outcome["context_chars"] = len(bounded)
        outcome["context_tokens"] = estimate_tokens(len(bounded.encode("utf-8")))
    return outcome


def _alert_suffix(outcome: dict) -> str:
    return "\n".join(outcome.get("alerts") or [])


def _unique_text(items: list[str]) -> list[str]:
    """Stable exact dedupe after whitespace normalization."""
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        normalized = item.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def _context_limit() -> int:
    try:
        return max(256, int(os.environ.get(
            "NXTHOOKS_CONTEXT_MAX_CHARS", DEFAULT_CONTEXT_MAX_CHARS
        )))
    except ValueError:
        return DEFAULT_CONTEXT_MAX_CHARS


def estimate_tokens(nbytes: int) -> int:
    """Deterministic size estimate in the shared ceil(bytes / 4) convention.

    Same denomination as the NxtAgents prompt budgets, so hook context and
    agent projections can be compared on one ledger. An estimate for catching
    growth, not a provider-tokenizer measurement.
    """
    if nbytes <= 0:
        return 0
    return (nbytes + 3) // 4


def _bounded_context_text(blocks: list[str]) -> str:
    """Compose compact context while leaving enforcement reasons untouched."""
    limit = _context_limit()
    marker = "\n\n[nxthooks] additional advisory context omitted"
    kept: list[str] = []
    for block in _unique_text(blocks):
        candidate = "\n\n".join((*kept, block))
        if len(candidate) <= limit:
            kept.append(block)
            continue
        if not kept:
            return block[: max(0, limit - len(marker))].rstrip() + marker
        current = "\n\n".join(kept)
        return current[: max(0, limit - len(marker))].rstrip() + marker
    return "\n\n".join(kept)


def normalize(harness: str, event: str, raw: dict) -> dict:
    """Map a harness envelope onto the canonical shape.

    Carries BOTH vocabularies ('tool'/'tool_name', 'args'/'tool_input') so
    hook sources written against Claude Code's native shape — including every
    pre-NxtHooks script — work unmodified.
    """
    tool = raw.get("tool") or raw.get("tool_name") or ""
    args = raw.get("args") if "args" in raw else raw.get("tool_input") or {}
    return {
        "version": 1,
        "harness": harness,
        "event": event,
        "cwd": raw.get("cwd") or "",
        "session_id": raw.get("session_id") or raw.get("sessionID") or raw.get("id") or "",
        "tool_call_id": raw.get("tool_call_id") or raw.get("tool_use_id") or raw.get("toolCallId") or raw.get("callID") or "",
        "success": (raw["success"] if isinstance(raw.get("success"), bool)
                    else not raw["isError"] if isinstance(raw.get("isError"), bool)
                    else True if raw.get("hook_event_name") == "PostToolUse"
                    else False if raw.get("hook_event_name") == "PostToolUseFailure"
                    else None),
        "tool": tool,
        "tool_name": tool,
        "args": args,
        "tool_input": args,
        "prompt": raw.get("prompt") or "",
        "raw": raw,
    }


def select_hooks(
    entries: list[dict], cfg: dict, harness: str, event: str, envelope: dict, preflight: bool = False
):
    """preflight=True: pi's turn-start check (no tool chosen yet). Selects
    ONLY pi_preflight-marked entries and skips the matcher check (there is
    no tool to match against). preflight=False is every other dispatch,
    including pi's post-hoc before_tool call from tool_execution_end -- that
    one excludes pi_preflight entries so they don't fire twice per turn."""
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        if event not in (entry.get("events") or []):
            continue
        if preflight:
            if not entry.get("pi_preflight"):
                continue
        elif harness == "pi" and event == "before_tool" and entry.get("pi_preflight"):
            continue
        if not harness_enabled(entry, harness):
            continue
        if not preflight and not matcher_matches(entry, envelope.get("tool") or None):
            continue
        cwd = envelope["cwd"] or os.getcwd()
        if not scope_allows(cfg, cwd):
            continue
        # The manifest's own policy wins; without one, the configured default.
        # "off" is not a softer advise: the hook does not run.
        policy = entry.get("policy") or cfg["policy"]["default"]
        if policy == "off":
            continue
        yield {**entry, "policy": policy}


def _command_for(src: Path) -> list[str]:
    """Build the argv that runs this hook source on this platform.

    Extension is checked before the shebang, and that order is the portable
    one: Windows has no execute bit and ignores shebang lines entirely, so a
    .py hook there must be launched as `sys.executable script.py` or it does
    not run at all. Naming the interpreter explicitly also pins a .py hook to
    the SAME python running the dispatcher, rather than whatever `python3` a
    shebang happens to resolve to.

    Shebang handling stays for everything else, because a hook may legitimately
    be any executable, and on POSIX its own shebang is the most honest
    statement of how to run it.
    """
    import shlex

    if src.suffix == ".py":
        return [sys.executable, str(src)]
    if src.suffix in (".sh", ".bash"):
        # bash is not on Windows by default; a .sh hook is a POSIX-only hook
        # and the capability report should say so rather than fail obscurely.
        return ["bash", str(src)]

    if os.access(src, os.X_OK):
        try:
            first = src.open("rb").readline().decode("utf-8", "replace").strip()
        except OSError:
            first = ""
        if first.startswith("#!"):
            shebang = first[2:].strip()
            parts = shlex.split(shebang)
            if parts and "/usr/bin/env" in parts[0]:
                return [parts[1] if len(parts) > 1 else "sh", str(src)]
            if parts:
                return [*parts, str(src)]
        return [str(src)]
    return [str(src)]


def run_hook(entry: dict, envelope: dict, deadline: float | None = None) -> dict:
    src = source_path(entry)
    started = time.monotonic()
    timeout_ms = int(entry.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    cut_by_deadline = deadline is not None and (deadline - started) * 1000 < timeout_ms
    if cut_by_deadline:
        timeout_ms = max(1, int((deadline - started) * 1000))
    result = {
        "hook": entry["name"],
        "policy": entry.get("policy", "enforce"),
        "fail": entry.get("fail", "open"),
        #: Did the source actually execute? A hook that ran and errored is a
        #: different fact from a hook whose source is not on this machine,
        #: and open failure is only defensible for the first.
        "ran": True,
        "verdict": "allow",
        "reason": "",
        "context": [],
        "error": "",
        "ms": 0,
    }
    if not src.is_file():
        # Nothing executed, so there is no verdict to fail open on. The
        # permissiveness is unchanged -- a fail-open hook still allows, and a
        # fail-closed one still blocks -- but the *claim* is not: `ran` is
        # false, `aggregate` files this under `unavailable`, and `doctor` can
        # no longer print a guard's name in the list of guards that ran. A
        # stranger was being told two guards were active on a machine that
        # held neither script.
        result["ran"] = False
        result["error"] = f"source missing: {src}"
        result["verdict"] = "block" if result["fail"] == "closed" else "allow"
        result["reason"] = f"[{entry['name']}] {result['error']}" if result["verdict"] == "block" else ""
        result["context"] = [] if result["verdict"] == "block" else [f"[nxthooks] {entry['name']}: {result['error']}"]
        result["ms"] = int((time.monotonic() - started) * 1000)
        return result

    env = dict(os.environ)
    env.update(
        NXTHOOKS_HARNESS=envelope.get("harness", ""),
        NXTHOOKS_EVENT=envelope.get("event", ""),
        NXTHOOKS_TOOL=envelope.get("tool", ""),
        NXTHOOKS_CWD=envelope.get("cwd", ""),
    )
    try:
        proc = subprocess.run(
            _command_for(src),
            input=json.dumps(envelope),
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
            env=env,
        )
    except subprocess.TimeoutExpired:
        result["error"] = (f"cut at the dispatch deadline after {timeout_ms}ms" if cut_by_deadline
                           else f"timed out after {timeout_ms}ms")
        result["ms"] = timeout_ms
        # A guard the transport's budget cut short is a guard that did not
        # decide; the transport itself refuses in that case, so the dispatcher
        # must not answer allow on its behalf. Its own timeout keeps `fail`.
        if result["fail"] == "closed" or (cut_by_deadline and result["policy"] == "enforce"):
            result["verdict"] = "block"
            result["reason"] = f"[{entry['name']}] blocked: {result['error']}"
        else:
            result["context"] = [f"[nxthooks] {entry['name']}: {result['error']}"]
        return result
    except OSError as exc:
        result["error"] = f"exec failed: {exc}"
        result["ms"] = int((time.monotonic() - started) * 1000)
        if result["fail"] == "closed":
            result["verdict"] = "block"
            result["reason"] = f"[{entry['name']}] blocked: {result['error']}"
        else:
            result["context"] = [f"[nxthooks] {entry['name']}: {result['error']}"]
        return result

    result["ms"] = int((time.monotonic() - started) * 1000)
    code = proc.returncode
    if code == 0:
        result["verdict"] = "allow"
        stdout = proc.stdout or ""
        result["context"] = [
            line[5:].strip() for line in stdout.splitlines() if line.startswith("CTX:")
        ]
        # Legacy CC-native sources emit their own hookSpecificOutput JSON.
        # Carry it verbatim so pre-NxtHooks scripts keep working unchanged.
        stripped = stdout.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and "hookSpecificOutput" in parsed:
                    result["native_cc"] = stripped
                    result["context"] = []
            except json.JSONDecodeError:
                pass
    elif code == 2:
        result["verdict"] = "block"
        result["reason"] = (proc.stderr or "").strip() or f"[{entry['name']}] blocked"
    else:
        result["error"] = (proc.stderr or proc.stdout or f"exit {code}").strip()
        if result["fail"] == "closed":
            result["verdict"] = "block"
            result["reason"] = f"[{entry['name']}] blocked (fail-closed): {result['error'][:400]}"
        else:
            result["verdict"] = "allow"
            result["context"] = [f"[nxthooks] {entry['name']} failed (fail-open): {result['error'][:200]}"]
    return result


def _run_order(selected: list[dict]) -> list[dict]:
    """Enforce hooks first, each tier in selection order."""
    return sorted(selected, key=lambda entry: entry.get("policy", "enforce") != "enforce")


def _unrun_guard(entry: dict) -> dict:
    """An enforce hook the deadline left no time for refuses, for the same reason a cut one does."""
    return {"hook": entry["name"], "policy": "enforce", "fail": entry.get("fail", "open"), "ran": False,
            "verdict": "block", "reason": f"[{entry['name']}] blocked: not run, the dispatch deadline had passed",
            "context": [], "error": "not run: dispatch deadline passed", "ms": 0}


def run_hooks(selected: list[dict], envelope: dict, deadline: float | None = None) -> tuple[list[dict], list[str]]:
    """Run the selected hooks, enforce-policy hooks first, and stop at the first enforce veto.

    Nothing after an enforce veto can change the verdict, and every hook still
    running is time the transport may not grant: on 2026-10-08 pi stopped
    listening at 9s while an advise hook that ran after nxtgit-guard's veto was
    still hung, read the silence as allow, and the refused write landed.
    `deadline` (monotonic seconds) is the transport's budget: hooks are cut to
    fit it and any hook it leaves no time for is skipped. Returns the results
    and the names of the hooks that did not run."""
    ordered = _run_order(selected)
    results: list[dict] = []
    for index, entry in enumerate(ordered):
        if deadline is not None and time.monotonic() >= deadline:
            rest = ordered[index:]
            results.extend(_unrun_guard(entry) for entry in rest if entry.get("policy", "enforce") == "enforce")
            return results, [entry["name"] for entry in rest]
        result = run_hook(entry, envelope, deadline)
        results.append(result)
        if result["verdict"] == "block" and result["policy"] == "enforce":
            return results, [rest["name"] for rest in ordered[index + 1:]]
    return results, []


def aggregate(results: list[dict]) -> dict:
    """Fold the per-hook results, keeping "ran" and "could not run" apart.

    `ran` used to hold every selected hook, including those whose source did
    not exist -- so every reader of this document, `doctor` included, was told
    a guard had executed when nothing had. Verdicts still aggregate over both
    lists: a fail-closed hook with a missing source blocks exactly as before.
    Only the claim about execution changed.
    """
    ran = [res for res in results if res.get("ran", True)]
    unavailable = [res for res in results if not res.get("ran", True)]
    final = {"verdict": "allow", "reason": "", "context": [], "ran": ran,
             "unavailable": unavailable}
    advisory_reasons: list[str] = []
    for res in results:
        final["context"].extend(res["context"])
        if res["verdict"] == "block":
            if res["policy"] == "advise":
                advisory_reasons.append(f"[advise] {res['reason']}")
            else:
                final["verdict"] = "block"
                if not final["reason"]:
                    final["reason"] = res["reason"]
    # Advisory denials carry more decision value than ordinary CTX output.
    # Put them first so the bounded projection retains them when possible.
    final["context"] = advisory_reasons + final["context"]
    deduped = _unique_text(final["context"])
    bounded = _bounded_context_text(deduped)
    final["context"] = [bounded] if bounded else []
    final["context_chars"] = len(bounded)
    final["context_tokens"] = estimate_tokens(len(bounded.encode("utf-8")))
    final["context_truncated"] = len("\n\n".join(deduped)) > len(bounded)
    return final


def emit_native_cc(event: str, outcome: dict) -> int:
    if outcome["verdict"] == "block":
        print(outcome["reason"], file=sys.stderr)
        # A veto carries only its reason, so the failure of some *other* hook
        # in the same chain is invisible here unless it is appended. That is
        # how a permanently broken hook stayed hidden behind a correctly
        # blocking one.
        suffix = _alert_suffix(outcome)
        if suffix:
            print(suffix, file=sys.stderr)
        return 2

    bare_native: str | None = None
    for res in outcome["ran"]:
        native = res.get("native_cc")
        if native:
            try:
                payload = json.loads(native)
            except json.JSONDecodeError:
                continue
            extra = ((payload.get("hookSpecificOutput") or {}).get("additionalContext") or "").strip()
            if not extra and bare_native is None:
                bare_native = native

    context_text = _bounded_context_text(_context_blocks(outcome))
    if not context_text:
        if bare_native:
            print(bare_native)
        return 0
    cc_context_events = {"session_start": "SessionStart", "subagent_start": "SubagentStart",
                         "prompt_submit": "UserPromptSubmit"}
    if event in cc_context_events:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": cc_context_events[event],
                        "additionalContext": context_text,
                    }
                }
            )
        )
    else:
        print(context_text, file=sys.stderr)
    return 0


def _context_blocks(outcome: dict) -> list[str]:
    """Compose shared and legacy-native context without dropping siblings."""
    blocks: list[str] = []
    advisory_reasons: list[str] = []
    # Alerts lead. They are already throttled to one per hook per hour by
    # `record_failures`, and the native emitters build the allow path from
    # these blocks rather than from `outcome["context"]` -- so a failure note
    # attached only to `context` reaches the json adapters and vanishes on the
    # harness the fleet actually runs. Verified by demo, not by argument.
    blocks.extend(outcome.get("alerts") or [])
    # Both lists: a hook whose source is missing produces the one note that
    # tells the operator so, and dropping it here would make the honest
    # accounting in `aggregate` invisible at exactly the surface that matters.
    for res in (*outcome["ran"], *(outcome.get("unavailable") or [])):
        native = res.get("native_cc")
        if native:
            try:
                payload = json.loads(native)
            except json.JSONDecodeError:
                payload = {}
            extra = ((payload.get("hookSpecificOutput") or {}).get("additionalContext") or "").strip()
            if extra:
                blocks.append(extra)
        lines = [c.strip() for c in res["context"] if c.strip()]
        if lines:
            blocks.append("\n".join(lines))
        if res["verdict"] == "block" and res["policy"] == "advise" and res["reason"]:
            advisory_reasons.append(f"[advise] {res['reason']}")
    return advisory_reasons + blocks


def emit_native_codex(event: str, outcome: dict) -> int:
    """Emit the event-specific Codex 0.151 hook protocol.

    PreToolUse, UserPromptSubmit, PostToolUse, and Stop all define exit 2 as
    their synchronous blocking/feedback signal. SessionStart instead uses the
    shared JSON continuation fields; SessionEnd is advisory-only.
    """
    if outcome["verdict"] == "block":
        suffix = _alert_suffix(outcome)
        if event == "session_start":
            print(json.dumps({"continue": False, "stopReason": outcome["reason"]}))
            return 0
        if event != "session_end":
            print(outcome["reason"], file=sys.stderr)
            if suffix:
                print(suffix, file=sys.stderr)
            return 2

    context = _bounded_context_text(_context_blocks(outcome))
    if not context or event == "session_end":
        return 0

    event_names = {
        "session_start": "SessionStart",
        "prompt_submit": "UserPromptSubmit",
        "before_tool": "PreToolUse",
        "after_tool": "PostToolUse",
    }
    native_event = event_names.get(event)
    if native_event:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": native_event,
                "additionalContext": context,
            }
        }))
    else:
        # Stop has no additionalContext field. A systemMessage is the honest
        # advisory surface; returning plain text is invalid for this event.
        print(json.dumps({"systemMessage": context}))
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    started = time.monotonic()
    try:
        raw = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        if args.harness in ("claude-code", "codex", "muse") and args.out == "native":
            print(f"[nxthooks] unparseable payload, allowing: {exc}", file=sys.stderr)
            return 0
        print(json.dumps({"verdict": "allow", "error": f"unparseable payload: {exc}", "ran": []}))
        return 0

    event = (
        args.event
        or getattr(args, "event_positional", "")
        or (raw.get("event") if isinstance(raw, dict) else "")
        or ""
    )
    envelope = normalize(args.harness, event, raw)
    if not event:
        print(json.dumps({"verdict": "allow", "error": "no event specified", "ran": []}))
        return 0

    cfg = load_config(cwd=envelope["cwd"] or None)
    entries = load_manifests()
    selected = list(select_hooks(entries, cfg, args.harness, event, envelope, preflight=args.preflight))
    deadline = None
    deadline_ms = getattr(args, "deadline_ms", 0)
    if deadline_ms:
        deadline = started + deadline_ms / 1000
    if deadline_ms and args.out == "json":
        # Only a transport that passed --deadline-ms reads more than one line;
        # it needs this one to tell a guard it gave up on from no guard at all.
        order = _run_order(selected)
        print(json.dumps({"selected": [entry["name"] for entry in order],
                          "enforce": any(entry.get("policy", "enforce") == "enforce" for entry in order)}), flush=True)
    results, skipped = run_hooks(selected, envelope, deadline)
    outcome = aggregate(results)
    outcome["skipped"] = skipped
    #: After aggregation, before any emitter: the record is what makes a
    #: permanently failing hook visible on a path that drops context.
    note_failures(outcome, record_failures(results))

    if args.out == "json":
        print(json.dumps(outcome))
        return 0
    if args.harness in ("claude-code", "muse"):
        # Muse Code declares the same settings.json hooks shape as Claude
        # Code (SessionStart/UserPromptSubmit/PreToolUse, identical nesting)
        # and its blocking contract -- exit 2, stderr as the reason -- was
        # verified against Muse Code 1.3.0; Muse is wired by hand to call
        # `nxthooks dispatch --harness muse --out native`. The
        # allow-path context-injection shape is inferred from that same
        # config compatibility, not independently verified.
        return emit_native_cc(event, outcome)
    if args.harness == "codex":
        return emit_native_codex(event, outcome)
    print(json.dumps(outcome))
    return 0


def register(sub: argparse.ArgumentParser) -> None:
    p = sub.add_parser("dispatch", help="run all hooks matching harness+event")
    p.add_argument(
        "--harness",
        required=True,
        help=f"calling harness; any name dispatches (known: {', '.join(HARNESSES)}), unknown ones get the json outcome",
    )
    p.add_argument("event_positional", nargs="?", default="", help="canonical event as positional (transports)")
    p.add_argument("--event", default="", help=f"canonical event flag ({', '.join(CANONICAL_EVENTS)})")
    p.add_argument("--out", default="json", choices=("json", "native"))
    p.add_argument(
        "--preflight",
        action="store_true",
        help="select only pi_preflight-marked before_tool hooks, ignoring tool/matcher (pi turn-start check)",
    )
    p.add_argument(
        "--deadline-ms",
        type=int,
        default=0,
        help="the transport's own budget: hooks are cut to fit it, and a selection line precedes the outcome",
    )
    p.set_defaults(func=cmd_dispatch)
