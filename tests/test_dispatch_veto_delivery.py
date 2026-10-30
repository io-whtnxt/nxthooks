"""A veto the dispatcher has computed must reach the harness.

On 2026-10-08 a pi `write` to a shared-branch checkout was vetoed by
nxtgit-guard 2.3s into the call, and applied anyway: the dispatcher went on to
run snapshot-save (advise, hung that day), pi killed the dispatcher at its 9s
transport budget, read an empty stdout as `null`, and `null` allows. The guard
had already logged the call as enforced.

Two dispatcher properties close that: an enforce-policy veto ends the dispatch
(nothing after it can change the verdict, and a refused call has nothing to
snapshot), and a transport can hand the dispatcher its own budget so the
verdict is printed before the transport stops listening.
"""
import json
import subprocess
import sys
import time

from nxthooks import dispatch


def _hook(tmp_path, name, body, *, policy="enforce", fail="open", timeout_ms=10000):
    src = tmp_path / f"{name}.py"
    src.write_text("import sys, time\n" + body + "\n")
    return {"name": name, "source": str(src), "policy": policy, "fail": fail,
            "timeout_ms": timeout_ms, "events": ["before_tool"]}


BLOCK = "sys.stderr.write('refused'); sys.exit(2)"
HANG = "time.sleep(30)"
ALLOW = "sys.exit(0)"
ENVELOPE = {"harness": "pi", "event": "before_tool", "tool": "write", "cwd": "/tmp", "args": {}}


def test_an_enforce_veto_ends_the_dispatch(tmp_path):
    hooks = [_hook(tmp_path, "guard", BLOCK), _hook(tmp_path, "snapshot", HANG, policy="advise", timeout_ms=15000)]
    started = time.monotonic()
    results, skipped = dispatch.run_hooks(hooks, ENVELOPE)
    assert time.monotonic() - started < 5
    assert [r["hook"] for r in results] == ["guard"]
    assert skipped == ["snapshot"]
    assert dispatch.aggregate(results)["verdict"] == "block"


def test_enforce_hooks_run_before_advise_hooks_whatever_their_names(tmp_path):
    hooks = [_hook(tmp_path, "a-snapshot", HANG, policy="advise", timeout_ms=15000), _hook(tmp_path, "z-guard", BLOCK)]
    started = time.monotonic()
    results, skipped = dispatch.run_hooks(hooks, ENVELOPE)
    assert time.monotonic() - started < 5
    assert [r["hook"] for r in results] == ["z-guard"] and skipped == ["a-snapshot"]


def test_an_advise_block_does_not_end_the_dispatch(tmp_path):
    hooks = [_hook(tmp_path, "nag", BLOCK, policy="advise"), _hook(tmp_path, "guard", ALLOW)]
    results, skipped = dispatch.run_hooks(hooks, ENVELOPE)
    assert sorted(r["hook"] for r in results) == ["guard", "nag"] and skipped == []
    assert dispatch.aggregate(results)["verdict"] == "allow"


def test_a_deadline_cuts_a_slow_hook_short_and_applies_its_fail_mode(tmp_path):
    hooks = [_hook(tmp_path, "guard", ALLOW), _hook(tmp_path, "snapshot", HANG, policy="advise", timeout_ms=15000)]
    started = time.monotonic()
    results, skipped = dispatch.run_hooks(hooks, ENVELOPE, deadline=time.monotonic() + 1.5)
    assert time.monotonic() - started < 3
    snapshot = next(r for r in results if r["hook"] == "snapshot")
    assert "deadline" in snapshot["error"] and snapshot["verdict"] == "allow"


def test_a_fail_closed_hook_cut_by_the_deadline_still_blocks(tmp_path):
    hooks = [_hook(tmp_path, "guard", HANG, fail="closed")]
    results, _ = dispatch.run_hooks(hooks, ENVELOPE, deadline=time.monotonic() + 1)
    assert dispatch.aggregate(results)["verdict"] == "block"


def test_a_hook_the_deadline_leaves_no_time_for_is_skipped_not_run(tmp_path):
    hooks = [_hook(tmp_path, "snapshot", HANG, policy="advise")]
    results, skipped = dispatch.run_hooks(hooks, ENVELOPE, deadline=time.monotonic() - 1)
    assert results == [] and skipped == ["snapshot"]


def _dispatch_cli(tmp_path, *extra):
    """Run the real CLI with a manifest directory holding one blocking guard and one hanging advise hook."""
    user = tmp_path / "config" / "hooks" / "hooks.d"; user.mkdir(parents=True)
    guard = tmp_path / "guard.py"; guard.write_text("import sys\nsys.stderr.write('refused')\nsys.exit(2)\n")
    slow = tmp_path / "slow.py"; slow.write_text("import time\ntime.sleep(30)\n")
    (user / "t.toml").write_text(
        f'[[hook]]\nname = "z-guard"\nevents = ["before_tool"]\nmatchers = ["Write"]\nsource = "{guard}"\n'
        f'policy = "enforce"\nfail = "open"\ntimeout_ms = 10000\n\n'
        f'[[hook]]\nname = "a-slow"\nevents = ["before_tool"]\nmatchers = ["Write"]\nsource = "{slow}"\n'
        f'policy = "advise"\nfail = "open"\ntimeout_ms = 15000\n')
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "NXT_CONFIG_HOME": str(tmp_path / "config"),
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "NXT_STATE_HOME": str(tmp_path / "state"),
           "PYTHONPATH": str(dispatch.Path(dispatch.__file__).resolve().parents[1])}
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "nxthooks", "dispatch", "--harness", "pi", "--out", "json", *extra],
                          input=json.dumps({**ENVELOPE, "cwd": str(tmp_path)}), capture_output=True, text=True,
                          env=env, timeout=60)
    return proc, time.monotonic() - started


def test_the_cli_delivers_the_veto_without_waiting_on_the_slow_hook(tmp_path):
    proc, elapsed = _dispatch_cli(tmp_path)
    outcome = json.loads(proc.stdout.strip().splitlines()[-1])
    assert outcome["verdict"] == "block", proc.stdout + proc.stderr
    assert "a-slow" in outcome["skipped"]
    assert elapsed < 8


def test_with_a_deadline_the_cli_announces_its_selection_before_running_anything(tmp_path):
    """A transport that has to give up still needs to know whether a guard was in play."""
    proc, _ = _dispatch_cli(tmp_path, "--deadline-ms", "8000")
    first, last = (json.loads(line) for line in (proc.stdout.splitlines()[0], proc.stdout.splitlines()[-1]))
    assert first["enforce"] is True
    assert first["selected"].index("z-guard") < first["selected"].index("a-slow")     # enforce before advise
    assert last["verdict"] == "block"


def test_without_a_deadline_the_cli_prints_only_the_outcome(tmp_path):
    """Older transports JSON.parse the whole of stdout; a second line would read as null, which allows."""
    proc, _ = _dispatch_cli(tmp_path)
    assert len(proc.stdout.strip().splitlines()) == 1


def test_an_enforce_hook_cut_by_the_deadline_refuses_even_when_fail_open(tmp_path):
    """The transport refuses when its budget runs out under a guard; answering allow first would undo that."""
    hooks = [_hook(tmp_path, "guard", HANG, fail="open")]
    results, _ = dispatch.run_hooks(hooks, ENVELOPE, deadline=time.monotonic() + 1)
    assert dispatch.aggregate(results)["verdict"] == "block"


def test_an_enforce_hook_its_own_timeout_stops_keeps_its_fail_mode(tmp_path):
    hooks = [_hook(tmp_path, "guard", HANG, fail="open", timeout_ms=500)]
    results, _ = dispatch.run_hooks(hooks, ENVELOPE, deadline=time.monotonic() + 5)
    assert dispatch.aggregate(results)["verdict"] == "allow"


def test_an_enforce_hook_the_deadline_skips_refuses(tmp_path):
    hooks = [_hook(tmp_path, "guard", ALLOW, fail="open")]
    results, skipped = dispatch.run_hooks(hooks, ENVELOPE, deadline=time.monotonic() - 1)
    assert skipped == ["guard"] and dispatch.aggregate(results)["verdict"] == "block"
