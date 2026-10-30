"""A hook's policy decides whether it runs at all, not only how its veto counts.

`policy = "off"` was accepted by manifest validation and then ignored: the hook
was selected, executed, and its veto blocked like an enforce hook's, because
aggregate only downgrades "advise". And `[policy] default` (or NXTHOOKS_POLICY)
was validated and never read: a manifest without a policy was enforce whatever
the config said.
"""
from nxthooks import dispatch

ENVELOPE = {"harness": "claude-code", "event": "before_tool", "tool": "Write", "cwd": "/", "args": {}}


def _cfg(default="enforce"):
    return {"policy": {"default": default}, "scope": {}, "harnesses": {}}


def _entry(name, **kw):
    return {"name": name, "events": ["before_tool"], "source": "/nonexistent", **kw}


def _select(entries, cfg, monkeypatch):
    monkeypatch.setattr(dispatch, "scope_allows", lambda cfg, cwd: True)
    return list(dispatch.select_hooks(entries, cfg, "claude-code", "before_tool", ENVELOPE))


def test_a_hook_whose_policy_is_off_is_never_selected(monkeypatch):
    selected = _select([_entry("on", policy="enforce"), _entry("off", policy="off")], _cfg(), monkeypatch)
    assert [e["name"] for e in selected] == ["on"]


def test_a_hook_without_a_policy_takes_the_configured_default(monkeypatch):
    selected = _select([_entry("bare"), _entry("explicit", policy="enforce")], _cfg("advise"), monkeypatch)
    assert {e["name"]: e["policy"] for e in selected} == {"bare": "advise", "explicit": "enforce"}


def test_a_default_of_off_disables_only_hooks_that_do_not_say_otherwise(monkeypatch):
    selected = _select([_entry("bare"), _entry("explicit", policy="enforce")], _cfg("off"), monkeypatch)
    assert [e["name"] for e in selected] == ["explicit"]


def test_resolution_does_not_write_into_the_loaded_manifest(monkeypatch):
    bare = _entry("bare")
    _select([bare], _cfg("advise"), monkeypatch)
    assert "policy" not in bare
