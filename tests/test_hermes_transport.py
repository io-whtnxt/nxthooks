"""The Hermes transport translates verdicts into Hermes's own directive shapes.

Hermes loads plugins in-process as Python, so unlike the shell and TypeScript
transports these callbacks can be imported and driven directly. The contracts
asserted here were read off the installed hermes_agent source, not inferred
from how the other harnesses behave -- re-checked against 0.21.1 on
2026-09-15, where `hermes_cli.plugins._get_pre_tool_call_directive_details`
still consumes `{"action": "block", "message": <non-empty str>}` and turns
the message into the tool result.

The veto was also driven end-to-end through Hermes' own
`_dispatch_pre_tool_call_hooks` in its venv, which returned our block
message. That is not a committed test: Hermes runs on its own Python 3.11
and this suite does not, so a cross-interpreter import would be a fragile
gate on every run. The version is named above so the claim is falsifiable.
"""

import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "nxthooks" / "adapters" / "hermes-plugin" / "__init__.py"


def _load():
    spec = importlib.util.spec_from_file_location("nxthooks_hermes", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod(monkeypatch):
    m = _load()
    calls = []

    def fake_dispatch(event, payload):
        calls.append((event, payload))
        return fake_dispatch.outcome

    fake_dispatch.outcome = None
    monkeypatch.setattr(m, "_dispatch", fake_dispatch)
    m._calls = calls
    m._fake = fake_dispatch
    return m


def test_pre_tool_call_block_becomes_hermes_block_directive(mod):
    mod._fake.outcome = {"verdict": "block", "reason": "claim held by another actor",
                         "context": [], "ran": []}
    out = mod.on_pre_tool_call(tool_name="Bash", args={"command": "git commit"},
                               session_id="s1")
    assert out == {"action": "block", "message": "claim held by another actor"}
    event, payload = mod._calls[0]
    assert event == "before_tool"
    assert payload["tool_name"] == "Bash"
    assert payload["tool_input"] == {"command": "git commit"}


def test_pre_tool_call_allow_returns_none(mod):
    mod._fake.outcome = {"verdict": "allow", "reason": "", "context": [], "ran": []}
    assert mod.on_pre_tool_call(tool_name="Bash", args={}, session_id="s1") is None


def test_pre_tool_call_non_dict_args_do_not_crash(mod):
    mod._fake.outcome = {"verdict": "allow", "reason": "", "context": [], "ran": []}
    assert mod.on_pre_tool_call(tool_name="Bash", args="not-a-dict") is None
    assert mod._calls[0][1]["tool_input"] == {}


def test_prompt_submit_returns_context_directive(mod):
    mod._fake.outcome = {"verdict": "allow", "reason": "",
                         "context": ["**Route:** worktree at /x"], "ran": []}
    out = mod.on_pre_llm_call(user_message="continue the payables work", session_id="s1")
    assert out == {"context": "**Route:** worktree at /x"}
    assert mod._calls[0][0] == "prompt_submit"


def test_prompt_submit_block_degrades_to_warning(mod):
    """pre_llm_call cannot veto the call, so a block must surface as text."""
    mod._fake.outcome = {"verdict": "block", "reason": "wrong repo", "context": [], "ran": []}
    out = mod.on_pre_llm_call(user_message="x", session_id="s1")
    assert "wrong repo" in out["context"]
    assert "action" not in out


def test_prompt_submit_silent_when_nothing_to_say(mod):
    mod._fake.outcome = {"verdict": "allow", "reason": "", "context": [], "ran": []}
    assert mod.on_pre_llm_call(user_message="x", session_id="s1") is None


def test_dispatcher_failure_never_blocks(mod):
    mod._fake.outcome = None
    assert mod.on_pre_tool_call(tool_name="Bash", args={}) is None
    assert mod.on_pre_llm_call(user_message="x") is None


def test_session_end_dispatches_the_canonical_close_event(mod):
    mod._fake.outcome = {"verdict": "allow", "reason": "", "context": [], "ran": []}

    assert mod.on_session_end(session_id="s1", reason="shutdown", completed=False) is None

    event, payload = mod._calls[0]
    assert event == "session_end"
    assert payload == {
        "cwd": mod.os.getcwd(),
        "session_id": "s1",
        "reason": "shutdown",
        "completed": False,
    }


def test_register_wires_every_declared_hook():
    """plugin.yaml and register() must not drift apart."""
    m = _load()
    registered, sections = [], []

    class Ctx:
        def register_hook(self, name, cb):
            registered.append(name)

        def register_system_prompt_section(self, ident, content, **kw):
            sections.append(ident)

    m.register(Ctx())
    declared = (PLUGIN.parent / "plugin.yaml").read_text()
    for name in registered:
        assert f"- {name}" in declared, f"{name} registered but not declared in plugin.yaml"
    assert registered == [
        "pre_llm_call", "pre_tool_call", "post_tool_call", "on_session_end"
    ]
    assert sections == ["nxthooks.session"]


def test_real_dispatcher_is_invoked_out_of_process(tmp_path, monkeypatch):
    """The transport shells to the shared runner rather than importing it.

    Keeps every harness on one dispatcher; a Hermes-only code path would be
    the fork this directory exists to prevent.
    """
    m = _load()
    runner = tmp_path / "run_nxthooks.py"
    runner.write_text(
        "import json,sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'verdict':'block','reason':'from-runner','context':[],'ran':[]}))\n"
    )
    monkeypatch.setattr(m, "RUNNER", runner)
    out = m.on_pre_tool_call(tool_name="Bash", args={})
    assert out == {"action": "block", "message": "from-runner"}


def test_missing_runner_is_fail_open(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "RUNNER", tmp_path / "absent.py")
    assert m.on_pre_tool_call(tool_name="Bash", args={}) is None


def test_a_block_with_no_reason_still_carries_a_message(mod):
    """Hermes drops a block whose message is empty, and runs the tool.

    `_get_pre_tool_call_directive_details` (hermes_agent 0.21.1) reads
    `if action == "block" and not message: continue` -- a veto with nothing
    to say is skipped entirely, not honoured with a blank reason. So a hook
    that refuses without explaining itself would be silently ignored on
    Hermes alone, while blocking on all four other harnesses.
    """
    mod._fake.outcome = {"verdict": "block", "reason": "", "context": [], "ran": []}

    out = mod.on_pre_tool_call(tool_name="Bash", args={"command": "x"},
                               session_id="s1")

    assert out["action"] == "block"
    assert out["message"], "an empty message makes Hermes ignore the block"
