"""A payload larger than max_bytes cannot be scanned completely: the CLI must
answer the way the hook does (hook.py refuses under enforce)."""
import io
import json

import pytest

from nxthooks.engines.nxtgrep import cli
from nxthooks.engines.nxtgrep.config import GrepConfig


@pytest.mark.parametrize("policy, code, decision", [("enforce", 3, "refuse"), ("advise", 0, "continue")])
def test_an_unscannable_payload_follows_policy(monkeypatch, capsys, policy, code, decision):
    monkeypatch.setattr(cli, "load_config", lambda: GrepConfig(policy=policy, max_bytes=64))
    clean = {"tool_name": "Write", "tool_input": {"content": "x" * 4096}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(clean)))

    assert cli.cmd_scan() == code
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["decision"] == decision
    assert "cannot be scanned completely" in verdict["reason"]
