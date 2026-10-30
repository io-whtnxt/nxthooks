"""pi's tool_call gate must deliver a veto, and must not read its own timeout as consent.

On 2026-10-08 nxtgit-guard vetoed a pi write 2.3s in; a hung advise hook kept
the dispatcher alive past the adapter's 9s timer, the adapter read the empty
stdout as null, and null allowed the write. These drive the real adapter
through its `tool_call` handler against scripted and real dispatchers.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PI_ADAPTER = ROOT / "nxthooks" / "adapters" / "pi-extension" / "index.ts"

TOOL_CALL = """
import { pathToFileURL } from 'node:url';
const { default: register } = await import(pathToFileURL(process.env.ADAPTER).href);
const handlers = new Map();
register({ on(name, handler) { handlers.set(name, handler); } });
const ctx = { cwd: process.env.CALL_CWD, sessionManager: { getSessionId: () => 's1' }, ui: { notify: async () => {} } };
const verdict = await handlers.get('tool_call')({ toolName: 'write', toolCallId: 't1', input: { path: 'x' } }, ctx);
console.log(JSON.stringify(verdict ?? null));
"""


def _tool_call(tmp_path: Path, runner_body: str, timeout_ms: int = 1500) -> tuple[dict | None, float]:
    (tmp_path / "lib").mkdir(exist_ok=True)
    (tmp_path / "lib" / "run_nxthooks.py").write_text(runner_body)
    env = {**os.environ, "NXTHOOKS_ROOT": str(tmp_path), "ADAPTER": str(PI_ADAPTER), "CALL_CWD": str(tmp_path),
           "NXTHOOKS_DISPATCH_TIMEOUT_MS": str(timeout_ms), "NXTHOOKS_ARGV": str(tmp_path / "argv.json")}
    started = time.monotonic()
    proc = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", TOOL_CALL],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1]), time.monotonic() - started


def _scripted(*lines: dict, hang: bool = False) -> str:
    out = "".join(f"print({json.dumps(json.dumps(line))}, flush=True)\n" for line in lines)
    return ("import json, os, sys, time\njson.load(sys.stdin)\n"
            "open(os.environ['NXTHOOKS_ARGV'], 'w').write(json.dumps(sys.argv[1:]))\n"
            + out + ("time.sleep(30)\n" if hang else ""))


def test_a_timeout_while_a_guard_was_selected_refuses_the_call(tmp_path):
    verdict, _ = _tool_call(tmp_path, _scripted({"selected": ["nxtgit-guard"], "enforce": True}, hang=True))
    assert verdict["block"] is True and "did not answer" in verdict["reason"]


def test_a_timeout_with_no_guard_selected_lets_the_call_through(tmp_path):
    verdict, _ = _tool_call(tmp_path, _scripted({"selected": ["snapshot-save"], "enforce": False}, hang=True))
    assert verdict is None


def test_a_timeout_before_the_selection_is_known_refuses_the_call(tmp_path):
    verdict, _ = _tool_call(tmp_path, _scripted(hang=True))
    assert verdict["block"] is True


def test_the_dispatcher_is_handed_a_deadline_inside_the_timer(tmp_path):
    verdict, _ = _tool_call(tmp_path, _scripted({"selected": ["g"], "enforce": True},
                                                {"verdict": "block", "reason": "refused by g"}), timeout_ms=9000)
    assert verdict == {"block": True, "reason": "refused by g"}
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[argv.index("--deadline-ms") + 1] == "7500"


def test_a_real_dispatch_delivers_the_veto_despite_a_hung_advise_hook(tmp_path):
    """The 2026-10-08 shape end to end: guard vetoes, an advise hook would hang 30s, the timer is 4s."""
    hooks = tmp_path / "config" / "hooks" / "hooks.d"; hooks.mkdir(parents=True)
    (tmp_path / "guard.py").write_text("import sys\nsys.stderr.write('shared branch: refused')\nsys.exit(2)\n")
    (tmp_path / "hang.py").write_text("import time\ntime.sleep(30)\n")
    (hooks / "t.toml").write_text(
        f'[[hook]]\nname = "z-guard"\nevents = ["before_tool"]\nmatchers = ["Write"]\nsource = "{tmp_path / "guard.py"}"\n'
        f'policy = "enforce"\nfail = "open"\ntimeout_ms = 10000\n\n'
        f'[[hook]]\nname = "a-snapshot"\nevents = ["before_tool"]\nmatchers = ["Write"]\nsource = "{tmp_path / "hang.py"}"\n'
        f'policy = "advise"\nfail = "open"\ntimeout_ms = 15000\n')
    runner = (f"import os, sys\nos.environ['NXT_CONFIG_HOME'] = {str(tmp_path / 'config')!r}\n"
              f"os.environ['NXT_STATE_HOME'] = {str(tmp_path / 'state')!r}\n"
              f"sys.path.insert(0, {str(ROOT)!r})\nfrom nxthooks.cli import main\nraise SystemExit(main())\n")
    verdict, elapsed = _tool_call(tmp_path, runner, timeout_ms=4000)
    assert verdict == {"block": True, "reason": "shared branch: refused"}
    assert elapsed < 4


def test_a_dispatcher_that_dies_after_announcing_a_guard_refuses_the_call(tmp_path):
    """The selection line parses as JSON too; read as an outcome it has no verdict, which allowed."""
    verdict, _ = _tool_call(tmp_path, _scripted({"selected": ["nxtgit-guard"], "enforce": True}) + "sys.exit(1)\n",
                            timeout_ms=9000)
    assert verdict["block"] is True


def test_a_budget_below_the_margin_still_hands_the_dispatcher_a_positive_deadline(tmp_path):
    _tool_call(tmp_path, _scripted({"selected": [], "enforce": False}, {"verdict": "allow"}), timeout_ms=1000)
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert int(argv[argv.index("--deadline-ms") + 1]) > 0


def _rejects_deadline(answer: dict | None) -> str:
    """A dispatcher older than the adapter: an unknown --deadline-ms is a usage error (exit 1, empty stdout)."""
    reply = f"print({json.dumps(json.dumps(answer))})\n" if answer else "sys.exit(1)\n"
    return ("import json, os, sys\njson.load(sys.stdin)\n"
            "open(os.environ['NXTHOOKS_ARGV'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if '--deadline-ms' in sys.argv: sys.exit(1)\n" + reply)


def test_an_older_dispatcher_is_asked_again_without_the_deadline(tmp_path):
    """Found in review of the Muse adapter: the adapter read the old dispatcher's empty reply as null, and null allowed."""
    verdict, _ = _tool_call(tmp_path, _rejects_deadline({"verdict": "block", "reason": "refused by g"}), timeout_ms=9000)
    assert verdict == {"block": True, "reason": "refused by g"}
    calls = [json.loads(line) for line in (tmp_path / "argv.json").read_text().splitlines()]
    assert "--deadline-ms" in calls[0] and "--deadline-ms" not in calls[1]


def test_a_dispatcher_that_never_answers_refuses_the_call(tmp_path):
    verdict, _ = _tool_call(tmp_path, _rejects_deadline(None), timeout_ms=9000)
    assert verdict["block"] is True and "no answer" in verdict["reason"]
