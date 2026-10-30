"""Behavior and safety tests for the portable NLM session-start adapter."""

from __future__ import annotations

import importlib.util
import io
import json
import re
from pathlib import Path


ADAPTER = Path(__file__).parents[1] / "nxthooks" / "adapters" / "nlm-session-start.py"
SPEC = importlib.util.spec_from_file_location("nxthooks_nlm_session_start", ADAPTER)
assert SPEC and SPEC.loader
nlm_session_start = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nlm_session_start)


class _Response:
    def __init__(self, body: dict):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.body).encode("utf-8")


def test_marker_path_hashes_untrusted_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(nlm_session_start, "STATE_DIR", tmp_path)

    marker = nlm_session_start.marker_path("../../outside/session")

    assert marker.parent == tmp_path
    assert re.fullmatch(r"[0-9a-f]{64}\.fired", marker.name)


def test_claim_session_is_atomic_and_fail_open(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(nlm_session_start, "STATE_DIR", state_dir)

    assert nlm_session_start.claim_session("session-1") is True
    assert nlm_session_start.claim_session("session-1") is False
    assert len(list(state_dir.iterdir())) == 1

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied")
    monkeypatch.setattr(nlm_session_start, "STATE_DIR", blocked)
    assert nlm_session_start.claim_session("session-2") is False


def test_main_emits_pointer_block_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(nlm_session_start, "STATE_DIR", tmp_path)
    monkeypatch.setattr(nlm_session_start, "load_token", lambda: "secret-token")
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return _Response({
            "results": [{
                "id": "session-42",
                "label": "Prior decision",
                "startedAt": "2026-09-20T12:00:00Z",
                "summary": "Keep the verified installer boundary.",
            }]
        })

    monkeypatch.setattr(nlm_session_start.urllib.request, "urlopen", fake_urlopen)
    payload = json.dumps({
        "session_id": "muse/session-1",
        "prompt": "How should Muse hooks be installed?",
        "harness": "muse",
    })

    monkeypatch.setattr(nlm_session_start.sys, "stdin", io.StringIO(payload))
    assert nlm_session_start.main() == 0
    output = capsys.readouterr().out
    assert "CTX: ## Possibly-relevant prior sessions" in output
    assert "session-42 · Prior decision (2026-09-20)" in output
    assert calls[0][0].get_header("Authorization") == "Bearer secret-token"
    assert calls[0][0].get_header("X-recall-runtime") == "muse"

    monkeypatch.setattr(nlm_session_start.sys, "stdin", io.StringIO(payload))
    assert nlm_session_start.main() == 0
    assert capsys.readouterr().out == ""
    assert len(calls) == 1


def test_summary_truncation_prefers_sentence_boundary():
    text = "A" * 70 + ". " + "B" * 200
    assert nlm_session_start.truncate_summary(text, max_len=100) == "A" * 70 + "."
