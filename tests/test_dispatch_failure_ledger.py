"""A hook that fails every run has to be findable, not merely survivable.

On 2026-10-08 every `policy = "enforce"` file-write guard on a machine was
believed dead because a dispatcher pre-step failed and was reported once, as a
context note, on a path that discards context. The pre-step turned out to be
`fail = "open"` on purpose and the guards had been running the whole time -- but
nothing could have answered the question either way. `doctor` verified that the
failing hook's *file* existed; it had failed on every one of its runs and said
so into channels that keep nothing.

These tests cover the three things that replace the silence: a durable per-hook
record, an announcement that reaches the model at most hourly, and the record
being visible on the two surfaces an operator reads -- a blocked call, and
`verify`.
"""
import json
import time

import pytest

from nxthooks.dispatch import (
    ANNOUNCE_SECONDS,
    aggregate,
    emit_native_cc,
    emit_native_codex,
    failure_ledger_path,
    note_failures,
    read_failure_ledger,
    record_failures,
)
from nxthooks.lifecycle import _report_hook_failures


@pytest.fixture(autouse=True)
def _state(monkeypatch, tmp_path):
    monkeypatch.setenv("NXT_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def _failed(**kw):
    base = {
        "hook": "snapshot-save", "policy": "advise", "fail": "open", "ran": True,
        "verdict": "allow", "reason": "", "context": [], "ms": 12,
        "error": "git commit -q failed: leaks found: 4",
    }
    base.update(kw)
    return base


def _ok(**kw):
    base = {"hook": "nxtgit-guard", "policy": "enforce", "fail": "closed", "ran": True,
            "verdict": "allow", "reason": "", "context": [], "error": "", "ms": 5}
    base.update(kw)
    return base


def test_a_failure_is_written_down_where_it_outlives_the_call():
    alerts = record_failures([_failed()])
    entry = read_failure_ledger()["snapshot-save"]
    assert entry["count"] == 1
    assert "leaks found: 4" in entry["last_error"]
    assert entry["policy"] == "advise" and entry["fail"] == "open"
    assert alerts and "1 time since" in alerts[0]
    assert failure_ledger_path().is_file()


def test_counts_accumulate_but_the_announcement_is_throttled():
    now = time.time()
    for _ in range(50):
        record_failures([_failed()], now=now)
    assert read_failure_ledger()["snapshot-save"]["count"] == 50
    # the 50th failure says nothing new this hour...
    assert record_failures([_failed()], now=now + 60) == []
    # ...and the next hour says it again, with the running total.
    later = record_failures([_failed()], now=now + ANNOUNCE_SECONDS + 1)
    assert len(later) == 1 and "52 time" in later[0]


def test_a_hook_that_recovers_stops_being_announced_but_keeps_its_history():
    record_failures([_failed(hook="secret-scan")])
    assert read_failure_ledger()["secret-scan"]["count"] == 1
    record_failures([_ok(hook="secret-scan")])
    assert read_failure_ledger()["secret-scan"]["count"] == 1  # not a failure


def test_a_missing_source_is_recorded_as_a_failure():
    """`ran = false` is the loudest case: the guard is not on this machine."""
    record_failures([_failed(ran=False, error="source missing: /nope/gone.py")])
    entry = read_failure_ledger()["snapshot-save"]
    assert entry["ran"] is False and "source missing" in entry["last_error"]


def test_a_broken_ledger_never_breaks_a_call(monkeypatch, tmp_path):
    """The messenger must not become the outage -- it would invert fail=open."""
    monkeypatch.setenv("NXT_STATE_HOME", str(tmp_path / "a-file"))
    (tmp_path / "a-file").write_text("not a directory")
    alerts = record_failures([_failed()])  # raises nothing
    assert len(alerts) == 1
    assert read_failure_ledger() == {}


def test_a_damaged_ledger_is_tolerated(tmp_path):
    path = failure_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert read_failure_ledger() == {}
    assert record_failures([_failed()])  # rebuilds, does not raise


def test_a_veto_still_reports_the_other_hook_being_dead(capsys):
    """The exact blind spot: a correct block hid the broken hook behind it."""
    outcome = aggregate([
        _failed(hook="snapshot-save"),
        _ok(hook="nxtgit-guard", verdict="block", reason="write on a task branch"),
    ])
    alerts = record_failures(outcome["ran"])
    note_failures(outcome, alerts)
    assert emit_native_cc("before_tool", outcome) == 2
    err = capsys.readouterr().err
    assert "write on a task branch" in err          # the veto, intact and first
    assert "snapshot-save" in err and "leaks found: 4" in err


def test_codex_veto_carries_it_too(capsys):
    outcome = aggregate([_failed(hook="snapshot-save"),
                         _ok(verdict="block", reason="no")])
    note_failures(outcome, record_failures(outcome["ran"]))
    assert emit_native_codex("before_tool", outcome) == 2
    assert "snapshot-save" in capsys.readouterr().err


def test_alerts_reach_the_json_channel_for_the_ts_adapters():
    outcome = aggregate([_failed()])
    note_failures(outcome, record_failures(outcome["ran"]))
    assert json.dumps(outcome)  # serialisable end to end
    assert any("snapshot-save" in c for c in outcome["context"])


def test_verify_prints_the_hook_that_fails_every_run(capsys):
    """`[ok] hook source` was the whole answer while the hook was 100% dead."""
    record_failures([_failed()])
    assert _report_hook_failures({"snapshot-save"}) == ["snapshot-save"]
    out = capsys.readouterr().out
    assert "[warn] hook 'snapshot-save' failed 1x" in out
    assert "leaks found: 4" in out
    assert "hook-failures.json" in out  # names where the record lives


def test_verify_goes_quiet_about_a_blip_that_settled(capsys):
    """A permanent warning about a resolved blip is how the report loses its
    reader, which is the failure mode this whole mechanism exists to treat."""
    import time as _t
    from nxthooks.dispatch import FAILURE_REPORT_WINDOW
    record_failures([_failed()], now=_t.time() - FAILURE_REPORT_WINDOW - 3600)
    assert _report_hook_failures({"snapshot-save"}) == []
    out = capsys.readouterr().out
    assert "[warn]" not in out
    assert "1 older entry" in out


def test_verify_goes_quiet_once_the_hook_runs_clean_again(capsys):
    """A failure followed by a clean run is settled; counting it as failing is
    the same false alarm as a blip outside the window."""
    record_failures([_failed(ran=False, error="source missing: /x/hook.py")])
    record_failures([_ok(hook="snapshot-save")])
    assert _report_hook_failures({"snapshot-save"}) == []
    out = capsys.readouterr().out
    assert "[warn]" not in out
    assert "1 recovered" in out


def test_a_restored_file_alone_is_not_recovery(capsys, tmp_path):
    """Existence proves nothing about a hook that has not run since: an empty
    placeholder at the path must not silence the warning."""
    script = tmp_path / "hook.py"
    record_failures([_failed(ran=False, error=f"source missing: {script}")])
    script.write_text("")
    assert _report_hook_failures({"snapshot-save"}) == ["snapshot-save"]
    assert "[warn] hook 'snapshot-save'" in capsys.readouterr().out


def test_a_failure_after_the_clean_run_warns_again():
    import time as _t
    start = _t.time()
    record_failures([_failed()], now=start)
    record_failures([_ok(hook="snapshot-save")], now=start + 1)
    record_failures([_failed()], now=start + 2)
    assert _report_hook_failures({"snapshot-save"}, now=start + 3) == ["snapshot-save"]


def test_a_clean_run_of_a_hook_with_no_history_writes_nothing():
    record_failures([_failed()])
    before = read_failure_ledger()
    record_failures([_ok(hook="nxtgit-guard")])
    assert read_failure_ledger() == before


def test_a_clean_run_prints_nothing_and_writes_no_ledger(capsys):
    assert record_failures([_ok(), _ok(hook="secret-scan")]) == []
    assert read_failure_ledger() == {}
    assert _report_hook_failures({"nxtgit-guard"}) == []
    assert capsys.readouterr().out == ""


def test_the_alert_reaches_the_native_allow_path(capsys):
    """The allow path is composed from per-hook blocks, not from `outcome
    ["context"]` -- so an alert that only lives in `context` is visible to the
    json adapters and invisible to the harness this fleet runs on. Found by
    watching a live dispatch, which is the only reason it is in this file."""
    outcome = aggregate([_failed(), _ok(hook="nxtgit-guard")])
    note_failures(outcome, record_failures(outcome["ran"]))
    assert emit_native_cc("prompt_submit", outcome) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "snapshot-save" in payload["hookSpecificOutput"]["additionalContext"]


def test_the_alert_reaches_a_native_before_tool_allow(capsys):
    outcome = aggregate([_failed()])
    note_failures(outcome, record_failures(outcome["ran"]))
    assert emit_native_cc("before_tool", outcome) == 0
    assert "has failed 1 time since" in capsys.readouterr().err


def test_the_alert_reaches_codex_on_the_allow_path(capsys):
    outcome = aggregate([_failed()])
    note_failures(outcome, record_failures(outcome["ran"]))
    assert emit_native_codex("before_tool", outcome) == 0
    assert "snapshot-save" in json.loads(capsys.readouterr().out)[
        "hookSpecificOutput"]["additionalContext"]


def test_a_quiet_run_still_emits_no_alert(capsys):
    outcome = aggregate([_ok()])
    note_failures(outcome, record_failures(outcome["ran"]))
    assert emit_native_cc("prompt_submit", outcome) == 0
    assert capsys.readouterr().out == ""
