"""Exit code 2 means BLOCK, and nothing else may claim it.

Every harness transport reads this process's exit status as a verdict:
`dispatch.py`'s module docstring states it, and Claude Code, Codex, pi and
Hermes all treat 2 on a pre-tool event as a veto with stderr shown to the
model.

argparse also exits 2, on any usage error. So a bad `--harness`, a flag a
newer transport passes to an older dispatcher, a typo in a hand-edited
config -- each one vetoed the user's tool call instead of failing open. It is
the exact inversion of `fail = "open"`: the policy says "if the hook system
is broken, get out of the way", and the argument parser said "deny".

Observed 2026-09-13: a Codex transport from `main` met an installed
dispatcher that predated the codex harness. Every tool call came back
"Blocked by hook" with an argparse usage message as the reason.
"""

import subprocess
import sys

import pytest

from nxthooks.cli import main

BLOCK = 2


def _run(*args):
    """Through the real process boundary, which is what a transport sees."""
    return subprocess.run(
        [sys.executable, "-m", "nxthooks", *args],
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("argv", [
    ["dispatch", "--harness", "not-a-harness", "--event", "before_tool"],
    ["dispatch", "--event", "before_tool"],                      # --harness missing
    ["dispatch", "--harness", "claude-code", "--out", "yaml"],   # bad --out
    ["no-such-subcommand"],
])
def test_a_usage_error_never_exits_with_the_block_code(argv):
    assert main(argv) != BLOCK, (
        f"{argv} exited {BLOCK}, which every transport reads as 'block this "
        "tool call'. A malformed invocation must fail open."
    )


@pytest.mark.parametrize("argv", [
    ["dispatch", "--harness", "not-a-harness", "--event", "before_tool"],
    ["no-such-subcommand"],
])
def test_the_same_holds_across_the_process_boundary(argv):
    """In-process return values are not what a transport reads."""
    assert _run(*argv).returncode != BLOCK


def test_a_usage_error_is_still_a_failure():
    """Failing open must not become succeeding silently: a caller that checks
    for success still has to see one."""
    assert main(["dispatch", "--harness", "not-a-harness"]) != 0


def test_help_still_exits_zero():
    """--help raises SystemExit(0); the guard must not turn it into an error."""
    assert _run("--help").returncode == 0


def test_a_valid_invocation_is_unaffected():
    proc = subprocess.run(
        [sys.executable, "-m", "nxthooks", "dispatch",
         "--harness", "claude-code", "--event", "before_tool", "--out", "json"],
        input='{"cwd":"/tmp","session_id":"exit-code-test","tool_name":"Read","tool_input":{}}',
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
