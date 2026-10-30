"""Exercise pi and OpenCode close-event registration through their real seams."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PI_ADAPTER = ROOT / "nxthooks" / "adapters" / "pi-extension" / "index.ts"
OPENCODE_ADAPTER = ROOT / "nxthooks" / "adapters" / "opencode-plugin" / "nxthooks.ts"


def _runner(tmp_path: Path) -> Path:
    runner = tmp_path / "run_nxthooks.py"
    runner.write_text(
        "import json, os, sys\n"
        "payload = json.load(sys.stdin)\n"
        "with open(os.environ['NXTHOOKS_CAPTURE'], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(payload) + '\\n')\n"
        "print(json.dumps({'verdict': 'allow', 'reason': '', 'context': [], 'ran': []}))\n",
        encoding="utf-8",
    )
    return runner


def _node(adapter: Path, script: str, tmp_path: Path) -> list[dict]:
    capture = tmp_path / "dispatch.jsonl"
    env = {
        **os.environ,
        "NXTHOOKS_ROOT": str(tmp_path),
        "NXTHOOKS_CAPTURE": str(capture),
        "ADAPTER": str(adapter),
    }
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "run_nxthooks.py").write_text(
        _runner(tmp_path).read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]


def test_pi_registers_session_shutdown_and_dispatches_session_end_once(tmp_path):
    calls = _node(
        PI_ADAPTER,
        """
        import { readFileSync } from 'node:fs';
        import { pathToFileURL } from 'node:url';
        const { default: register } = await import(pathToFileURL(process.env.ADAPTER).href);
        const handlers = new Map();
        register({ on(name, handler) { handlers.set(name, handler); } });
        if (!handlers.has('session_shutdown')) throw new Error('session_shutdown not registered');
        const ctx = {
          cwd: '/tmp/pi-project',
          sessionManager: { getSessionId: () => 'pi-session-1' },
        };
        await handlers.get('session_shutdown')({ reason: 'quit' }, ctx);
        await handlers.get('session_shutdown')({ reason: 'quit' }, ctx);
        console.log(readFileSync(process.env.NXTHOOKS_CAPTURE, 'utf8'));
        """,
        tmp_path,
    )

    assert len(calls) == 1
    assert calls[0]["harness"] == "pi"
    assert calls[0]["event"] == "session_end"
    assert calls[0]["session_id"] == "pi-session-1"
    assert calls[0]["reason"] == "quit"


def test_opencode_event_bus_maps_session_deleted_to_session_end_once(tmp_path):
    calls = _node(
        OPENCODE_ADAPTER,
        """
        import { readFileSync } from 'node:fs';
        import { pathToFileURL } from 'node:url';
        const { NxtHooksPlugin } = await import(pathToFileURL(process.env.ADAPTER).href);
        const hooks = await NxtHooksPlugin({ directory: '/tmp/opencode-project' });
        if (typeof hooks.event !== 'function') throw new Error('event hook not registered');
        const deleted = {
          id: 'event-1',
          type: 'session.deleted',
          properties: { info: { id: 'opencode-session-1', directory: '/tmp/opencode-project' } },
        };
        await hooks.event({ event: deleted });
        await hooks.event({ event: deleted });
        await hooks.event({ event: { ...deleted, type: 'session.idle' } });
        console.log(readFileSync(process.env.NXTHOOKS_CAPTURE, 'utf8'));
        """,
        tmp_path,
    )

    assert len(calls) == 1
    assert calls[0]["harness"] == "opencode"
    assert calls[0]["event"] == "session_end"
    assert calls[0]["session_id"] == "opencode-session-1"
    assert calls[0]["cwd"] == "/tmp/opencode-project"


def test_opencode_prompt_submit_preserves_session_identity(tmp_path):
    """NLM's first-prompt recall deduplicates by the dispatched session id."""
    calls = _node(
        OPENCODE_ADAPTER,
        """
        import { pathToFileURL } from 'node:url';
        const { NxtHooksPlugin } = await import(pathToFileURL(process.env.ADAPTER).href);
        const hooks = await NxtHooksPlugin({ directory: '/tmp/opencode-project' });
        await hooks['chat.message'](
          { sessionID: 'opencode-session-2' },
          {
            parts: [{ type: 'text', text: 'recall this context' }],
            message: { id: 'message-1', sessionID: 'opencode-session-2' },
          },
        );
        """,
        tmp_path,
    )

    assert len(calls) == 1
    assert calls[0]["event"] == "prompt_submit"
    assert calls[0]["session_id"] == "opencode-session-2"
