"""The runner-missing sentinel must not collide with a real dispatch outcome.

Two different things were spelled `unavailable` on the same object:

  * the dispatcher's own report -- `unavailable` is the *list of hooks that
    did not run* (`dispatch.py`: `[res for res in results if not
    res.get("ran", True)]`), present on every outcome, normally `[]`
  * the transport adapters' sentinel -- `{unavailable: true}`, meaning the
    dispatcher script itself is missing from disk

In JavaScript `[]` is truthy. So `if (outcome?.unavailable)` was true on
*every successful dispatch*, and both JS adapters take that branch before
they look at the verdict:

    if (res?.unavailable) { ...warn...; return; }   // <- always taken
    if (res.verdict === "block") { throw ... }      // <- never reached

The effect is not a cosmetic warning. No hook could block a tool call on
OpenCode or pi at all -- NxtGrep's secret scan included -- while every
surface (install, verify, list, the dispatcher's own JSON) reported the
hook as wired and running. It also emitted "hooks are NOT running this
session" on every single session, which is how it was found: an OpenCode
run repeated that line back in its answer while the hooks were demonstrably
running.

The Hermes adapter is immune, and not by luck of Python's truthiness: it
compares by *identity* (`outcome is RUNNER_MISSING`) against the one module
-level object it returns itself, which a dict parsed from the dispatcher's
JSON can never be. That is the property worth keeping, so it is pinned below
rather than merely noted -- an identity check cannot be fooled by any value
the dispatcher puts on the wire, whatever the key is called.

The fix for the JS side is a key that cannot collide, asserted here on both
sides: the dispatcher keeps `unavailable` for its report, and the sentinel
is `runner_missing`.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ADAPTERS = REPO / "nxthooks" / "adapters"

SENTINEL = "runner_missing"
#: The dispatcher's own key. An adapter that branches on this is reading a
#: list as a boolean.
REPORT_KEY = "unavailable"

TRANSPORTS = {
    "opencode": ADAPTERS / "opencode-plugin" / "nxthooks.ts",
    "pi": ADAPTERS / "pi-extension" / "index.ts",
    "hermes": ADAPTERS / "hermes-plugin" / "__init__.py",
}


def _dispatch(tmp_path: Path) -> dict:
    """A real dispatch against an empty hook set, as an adapter would run it."""
    env = {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO),
    }
    (tmp_path / ".config" / "nxt" / "hooks" / "hooks.d").mkdir(parents=True)
    proc = subprocess.run(
        [sys.executable, "-m", "nxthooks", "dispatch",
         "--harness", "opencode", "--out", "json"],
        input=json.dumps({"version": 1, "harness": "opencode",
                          "event": "before_tool", "cwd": str(tmp_path),
                          "tool": "bash", "args": {"command": "true"}}),
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode in (0, 2), proc.stderr
    return json.loads(proc.stdout)


def test_a_normal_outcome_carries_the_report_key_not_the_sentinel(tmp_path):
    """The two must be distinguishable on the wire, or no adapter can tell
    'nothing was unavailable' from 'the dispatcher is gone'."""
    outcome = _dispatch(tmp_path)
    assert REPORT_KEY in outcome, "the dispatcher's own report key went missing"
    assert isinstance(outcome[REPORT_KEY], list), (
        f"`{REPORT_KEY}` is the list of hooks that did not run; a boolean here "
        "would mean the two meanings have been merged again"
    )
    assert SENTINEL not in outcome, (
        f"the dispatcher must never emit `{SENTINEL}` -- it is the transport "
        "layer's sentinel for its own absence, and a dispatcher that answered "
        "at all is by definition present"
    )


def test_the_hermes_sentinel_is_compared_by_identity():
    """The strongest form of the fix, and the reason Hermes never broke.

    `outcome is RUNNER_MISSING` compares against the one object the adapter
    returns itself. No value the dispatcher can put on the wire -- under any
    key, of any type -- is that object, so the sentinel cannot be forged or
    collided with. A `==`/`.get()` check would reintroduce exactly the
    JS-side hazard the moment the key names converge again.
    """
    source = TRANSPORTS["hermes"].read_text()
    assert "is RUNNER_MISSING" in source, (
        "hermes must compare its sentinel by identity, not by key lookup"
    )
    assert "== RUNNER_MISSING" not in source


@pytest.mark.parametrize("harness", ["opencode", "pi"])
def test_no_transport_branches_on_the_dispatchers_report_key(harness):
    """`if (outcome?.unavailable)` is true for `[]` in JavaScript.

    Scoped to the JS adapters: Hermes is covered by the identity test above,
    which is a stronger property than avoiding one key name.
    """
    source = TRANSPORTS[harness].read_text()
    for probe in (f"?.{REPORT_KEY}", f'get("{REPORT_KEY}")', f'["{REPORT_KEY}"]'):
        assert probe not in source, (
            f"{harness} branches on `{probe}`, the dispatcher's list of "
            f"hooks that did not run -- use `{SENTINEL}` for the sentinel"
        )


@pytest.mark.parametrize("harness", ["opencode", "pi"])
def test_every_transport_uses_the_uncollidable_sentinel(harness):
    source = TRANSPORTS[harness].read_text()
    assert SENTINEL in source, (
        f"{harness} never mentions `{SENTINEL}`; it cannot report a missing "
        "dispatcher without reusing a key that means something else"
    )
