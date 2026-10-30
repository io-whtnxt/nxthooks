"""Hook context carries a token-denominated budget line item.

The char cap already truncates; what was missing is observability in the
same denomination as the NxtAgents prompt budgets, so hook cost and agent
cost land on one ledger. `aggregate` reports tokens and whether truncation
fired; both are deterministic for fixed inputs.
"""

from nxthooks.dispatch import aggregate, estimate_tokens


def _ran(**kw):
    base = {"hook": "h", "policy": "enforce", "fail": "open", "verdict": "allow",
            "reason": "", "context": [], "error": "", "ms": 1}
    base.update(kw)
    return base


def test_estimate_tokens_is_deterministic_and_empty_is_zero():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(-3) == 0
    assert estimate_tokens(4000) == 1000
    assert estimate_tokens(4001) == 1001


def test_aggregate_reports_tokens_without_truncation():
    outcome = aggregate([_ran(context=["short note"])])
    assert outcome["context"] == ["short note"]
    assert outcome["context_chars"] == len("short note")
    assert outcome["context_tokens"] == estimate_tokens(len("short note".encode("utf-8")))
    assert outcome["context_truncated"] is False


def test_aggregate_reports_tokens_and_truncation_flag(monkeypatch):
    monkeypatch.setenv("NXTHOOKS_CONTEXT_MAX_CHARS", "256")
    outcome = aggregate([_ran(context=["x" * 200, "y" * 200])])
    assert outcome["context_truncated"] is True
    assert outcome["context_chars"] <= 256
    assert outcome["context_tokens"] == estimate_tokens(
        len(outcome["context"][0].encode("utf-8")))
    assert outcome["context"][0].endswith("additional advisory context omitted")


def test_aggregate_budget_fields_are_byte_stable():
    first = aggregate([_ran(context=["stable", "stable", "  stable  "])])
    second = aggregate([_ran(context=["stable", "stable", "  stable  "])])
    assert first["context"] == second["context"] == ["stable"]
    assert first["context_tokens"] == second["context_tokens"]
    assert first["context_truncated"] == second["context_truncated"] is False
