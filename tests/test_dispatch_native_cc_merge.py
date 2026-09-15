"""Multiple hooks on one Claude Code event must compose, not race.

A legacy hook that emits its own hookSpecificOutput JSON used to be returned
verbatim, discarding the CTX context of every hook that ran beside it. Nothing
errored and no test covered the multi-hook case, so the newer hook was simply
silent -- the same class of failure as a hook whose output nothing parses.
"""

import json

from nxthooks.dispatch import aggregate, emit_native_cc


def _ran(**kw):
    base = {"hook": "h", "policy": "enforce", "fail": "open", "verdict": "allow",
            "reason": "", "context": [], "error": "", "ms": 1}
    base.update(kw)
    return base


def _emit(capsys, outcome, event="prompt_submit"):
    code = emit_native_cc(event, outcome)
    out = capsys.readouterr()
    return code, out


def test_native_and_ctx_hooks_both_survive(capsys):
    native = json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": "legacy report"}})
    outcome = {
        "verdict": "allow", "reason": "",
        "context": ["routed to worktree", "call EnterWorktree"],
        "ran": [
            _ran(hook="legacy", native_cc=native),
            _ran(hook="router", context=["routed to worktree", "call EnterWorktree"]),
        ],
    }
    code, out = _emit(capsys, outcome)
    assert code == 0
    payload = json.loads(out.out)["hookSpecificOutput"]["additionalContext"]
    assert "legacy report" in payload
    assert "routed to worktree" in payload
    assert "call EnterWorktree" in payload


def test_ctx_lines_of_one_hook_stay_one_block(capsys):
    outcome = {
        "verdict": "allow", "reason": "", "context": ["line one", "line two"],
        "ran": [_ran(hook="router", context=["line one", "line two"])],
    }
    _, out = _emit(capsys, outcome)
    payload = json.loads(out.out)["hookSpecificOutput"]["additionalContext"]
    assert payload == "line one\nline two"


def test_advise_downgrade_still_reaches_the_model(capsys):
    outcome = {
        "verdict": "allow", "reason": "", "context": ["[advise] soft warning"],
        "ran": [_ran(hook="soft", policy="advise", verdict="block",
                     reason="soft warning")],
    }
    _, out = _emit(capsys, outcome)
    payload = json.loads(out.out)["hookSpecificOutput"]["additionalContext"]
    assert "soft warning" in payload


def test_block_still_wins_and_returns_two(capsys):
    outcome = {"verdict": "block", "reason": "denied", "context": ["ignored"],
               "ran": [_ran(hook="guard", verdict="block", reason="denied")]}
    code, out = _emit(capsys, outcome)
    assert code == 2
    assert "denied" in out.err


def test_native_without_additional_context_is_not_dropped(capsys):
    native = json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}})
    outcome = {"verdict": "allow", "reason": "", "context": [],
               "ran": [_ran(hook="legacy", native_cc=native)]}
    code, out = _emit(capsys, outcome)
    assert code == 0
    assert json.loads(out.out)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_identical_context_blocks_are_emitted_once(capsys):
    outcome = {
        "verdict": "allow", "reason": "", "context": ["same", "same"],
        "ran": [
            _ran(hook="first", context=["same"]),
            _ran(hook="second", context=["same"]),
        ],
    }
    _, out = _emit(capsys, outcome)
    payload = json.loads(out.out)["hookSpecificOutput"]["additionalContext"]
    assert payload == "same"


def test_distinct_context_remains_ordered(capsys):
    outcome = {
        "verdict": "allow", "reason": "", "context": ["first", "second"],
        "ran": [
            _ran(hook="first", context=["first"]),
            _ran(hook="second", context=["second"]),
        ],
    }
    _, out = _emit(capsys, outcome)
    payload = json.loads(out.out)["hookSpecificOutput"]["additionalContext"]
    assert payload == "first\n\nsecond"


def test_advisory_context_is_bounded_without_losing_block_verdict(capsys, monkeypatch):
    monkeypatch.setenv("NXTHOOKS_CONTEXT_MAX_CHARS", "256")
    outcome = aggregate([
        _ran(hook="noisy", context=["x" * 600]),
    ])
    _, out = _emit(capsys, outcome)
    payload = json.loads(out.out)["hookSpecificOutput"]["additionalContext"]
    assert len(payload) <= 256
    assert "omitted" in payload

    blocked = aggregate([
        _ran(hook="guard", verdict="block", reason="critical denial"),
        _ran(hook="noise", context=["x" * 600]),
    ])
    code, out = _emit(capsys, blocked)
    assert code == 2
    assert "critical denial" in out.err


def test_advisory_denial_precedes_noisy_context_when_bounded(capsys, monkeypatch):
    monkeypatch.setenv("NXTHOOKS_CONTEXT_MAX_CHARS", "256")
    outcome = aggregate([
        _ran(hook="noise", context=["x" * 600]),
        _ran(hook="warning", verdict="block", policy="advise", reason="review this action"),
    ])
    _, out = _emit(capsys, outcome)
    payload = json.loads(out.out)["hookSpecificOutput"]["additionalContext"]
    assert payload.startswith("[advise] review this action")
    assert len(payload) <= 256
