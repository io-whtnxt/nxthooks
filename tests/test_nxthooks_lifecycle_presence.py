"""The three properties a stranger's machine measured, as unit tests.

`stranger/` proves these on a real clean Linux box in a container, which is
the reading that counts. These are the same claims at the level where a
regression is cheap to find: install selects by presence, uninstall leaves
nothing it created, and doctor cannot say a hook ran when its source is not
on the machine.
"""

from __future__ import annotations

import json

import pytest

from nxthooks import HARNESSES, lifecycle, registry
from nxthooks.dispatch import aggregate, run_hook
from nxthooks.projection import live_slots


class _Args:
    harnesses = None
    keep_runtime = False
    purge_config = True


def _args(**overrides):
    args = _Args()
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _listing(home):
    return {
        str(path.relative_to(home)) + ("/" if path.is_dir() else "")
        for path in home.rglob("*")
    }


# ------------------------------------------------ install selects by presence


def test_install_wires_only_the_harness_that_is_present(hooks_machine, capsys):
    """The definition of done for 0.3.0, item 4: on a Claude-Code-only
    machine, `nxthooks install` wires only Claude Code, and no `~/.pi`,
    `~/.codex`, `~/.hermes` or `~/.config/opencode` exists afterwards."""
    host = hooks_machine("claude-code")

    assert lifecycle.cmd_install(_args(), host) == 0
    capsys.readouterr()

    for absent in (".pi", ".codex", ".hermes", ".config/opencode"):
        assert not (host.home / absent).exists(), absent
    assert {record.harness for record in live_slots("hooks", home=host.home, env={})} \
        <= {"claude-code"}


def test_install_ignores_a_config_that_claims_an_absent_harness(hooks_machine, capsys):
    """The exact defect: selection used to read `[harnesses]`, so a config
    saying `pi = true` on a machine with no pi manufactured `~/.pi`."""
    host = hooks_machine("claude-code")
    host.config_path.parent.mkdir(parents=True)
    host.config_path.write_text(
        "[harnesses]\nclaude-code = true\ncodex = true\npi = true\n"
        "opencode = true\nhermes = true\n"
    )

    lifecycle.cmd_install(_args(), host)
    capsys.readouterr()

    assert not (host.home / ".pi").exists()
    assert not (host.home / ".config" / "opencode").exists()


def test_install_and_its_own_verify_agree_on_a_fresh_machine(hooks_machine, capsys):
    """A stranger's first install used to end exit 1: `cmd_install` read the
    config before `seed_config` wrote it, so it wired the built-in defaults
    and then verified against the seed."""
    host = hooks_machine("claude-code")
    assert not host.config_path.exists()

    assert lifecycle.cmd_install(_args(), host) == 0
    out = capsys.readouterr().out

    assert "GAPS FOUND" not in out
    assert "all wiring resolves" in out
    assert lifecycle.cmd_verify(_args(), host) == 0


def test_an_explicit_harness_is_an_assertion_and_is_reported_as_one(
    hooks_machine, capsys
):
    """`--harnesses pi` on a machine with no pi is the operator's call, and
    it wires. What it may not do is pretend pi was detected."""
    host = hooks_machine("claude-code")

    lifecycle.cmd_install(_args(harnesses=["claude-code", "pi"]), host)
    out = capsys.readouterr().out

    assert "detected  absent    pi" in out
    assert "WARNING   pi was named explicitly but was not detected" in out
    assert (host.pi_extension / "index.ts").is_file()


def test_the_config_records_what_was_detected(hooks_machine, capsys):
    host = hooks_machine("claude-code", "codex")
    lifecycle.cmd_install(_args(), host)
    capsys.readouterr()

    declared = host.config_path.read_text()
    assert "claude-code = true" in declared
    assert "codex = true" in declared
    assert "pi = false" in declared


# ---------------------------------------------------- uninstall leaves nothing


def test_builtin_hook_sources_are_written_on_the_host_being_installed(hooks_machine, capsys, monkeypatch, tmp_path):
    """The builtin writers run as subprocesses. They used to inherit this
    process's HOME and XDG roots instead of the Host's, so installing onto
    any other home wrote and then deleted the operator's real hook sources
    (the suite did exactly that to the developer's machine)."""
    operator = tmp_path / "operator"
    operator.mkdir()
    monkeypatch.setenv("HOME", str(operator))
    monkeypatch.setenv("XDG_DATA_HOME", str(operator / "xdg-data"))
    host = hooks_machine("claude-code")

    lifecycle.cmd_install(_args(), host)
    capsys.readouterr()

    assert (host.home / ".local/share/nxt/lint/bin/nxtlint-hook.py").is_file()
    assert _listing(operator) == set()


def test_uninstall_leaves_the_machine_as_it_found_it(hooks_machine, capsys):
    host = hooks_machine("claude-code")
    before = _listing(host.home)

    lifecycle.cmd_install(_args(), host)
    lifecycle.cmd_uninstall(_args(), host)
    capsys.readouterr()

    after = _listing(host.home)
    assert sorted(after - before) == []
    assert sorted(before - after) == []


def test_uninstall_removes_the_directories_install_created(hooks_machine, capsys):
    """The residue that survived: `install` builds `~/.local/share/nxt/hooks`
    and every parent on the way, `uninstall` removed the leaf, and four empty
    directories stayed behind on a machine that had none of them."""
    host = hooks_machine("claude-code")

    lifecycle.cmd_install(_args(), host)
    assert (host.home / ".local" / "share" / "nxt" / "hooks").is_dir()

    lifecycle.cmd_uninstall(_args(), host)
    capsys.readouterr()

    for path in (".local/share/nxt/hooks", ".local/share/nxt", ".local/share",
                 ".local", ".config/nxt", ".config"):
        assert not (host.home / path).exists(), path


def test_uninstall_keeps_a_directory_it_did_not_create(hooks_machine, capsys):
    """Emptiness is the guard, and the record is the licence. A directory
    NxtOS never made is never a candidate, even when it is empty and on the
    path to something NxtOS did make."""
    host = hooks_machine("claude-code")
    (host.home / ".config").mkdir(parents=True)
    (host.home / ".config" / "somebody-elses").mkdir()

    lifecycle.cmd_install(_args(), host)
    lifecycle.cmd_uninstall(_args(), host)
    capsys.readouterr()

    assert (host.home / ".config").is_dir()
    assert (host.home / ".config" / "somebody-elses").is_dir()


def test_a_recorded_directory_someone_else_has_used_is_kept(hooks_machine, capsys):
    """The record is the licence, emptiness is the guard, and the guard is
    the half that matters here: `~/.config/nxt` is ours to have created and
    is not ours to take away once another component lives in it."""
    host = hooks_machine("claude-code")
    lifecycle.cmd_install(_args(), host)

    neighbour = host.config_dir.parent / "wiki"
    neighbour.mkdir(parents=True)
    (neighbour / "config.toml").write_text("# somebody else's component\n")

    lifecycle.cmd_uninstall(_args(), host)
    capsys.readouterr()

    assert (neighbour / "config.toml").is_file()
    assert not host.config_dir.exists()


def test_the_seed_is_written_before_anything_reads_it(hooks_machine, capsys, monkeypatch):
    """`cmd_install` used to call `load_config()` before `seed_config()`, so
    the first install on a fresh machine ran against the built-in defaults
    and every later verb ran against the seed. Proved with a seed whose root
    differs from the default, because a seed that agrees with the defaults
    cannot show the ordering at all."""
    host = hooks_machine("claude-code")
    monkeypatch.setattr(
        lifecycle, "SEED_CONFIG",
        lifecycle.SEED_CONFIG.replace('root = "~/.local/share/nxt/hooks"',
                                      'root = "~/elsewhere/hooks"'),
    )

    lifecycle.cmd_install(_args(), host)
    capsys.readouterr()

    assert (host.home / "elsewhere" / "hooks" / "VERSION").is_file()
    assert not (host.home / ".local" / "share" / "nxt" / "hooks").exists()
    assert lifecycle.cmd_verify(_args(), host) == 0


def test_uninstall_returns_the_foreign_config_byte_identical(hooks_machine, capsys):
    host = hooks_machine("claude-code")
    original = json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "$HOME/bin/operators-own.sh"}]}]},
         "permissions": {"allow": ["Bash(ls:*)"]}},
        indent=2,
    ) + "\n"
    host.cc_settings.write_text(original, encoding="utf-8")

    lifecycle.cmd_install(_args(), host)
    lifecycle.cmd_uninstall(_args(), host)
    capsys.readouterr()

    assert host.cc_settings.read_text(encoding="utf-8") == original


def test_uninstall_leaves_nothing_on_a_machine_with_all_five(hooks_machine, capsys):
    """The owned-outright transports too: a `.pi/agent/extensions/nxthooks`
    tree, an `opencode/plugins/nxthooks.ts`, a `.hermes/plugins/nxthooks`,
    and the ledger that recorded all three."""
    host = hooks_machine("claude-code", "codex", "pi", "opencode", "hermes")
    before = _listing(host.home)

    lifecycle.cmd_install(_args(), host)
    assert (host.pi_extension / ".nxt-managed").is_file()
    assert host.oc_plugin.read_text().startswith("// nxt:hooks/")
    assert (host.hermes_plugin / ".nxt-managed").is_file()

    lifecycle.cmd_uninstall(_args(), host)
    capsys.readouterr()

    assert sorted(_listing(host.home) - before) == []
    assert sorted(before - _listing(host.home)) == []


def test_uninstall_closes_every_ledger_slot_it_removes(hooks_machine, capsys):
    """Deleting the file is not closing the slot. A removal the ledger never
    hears about leaves a projection that reads as live forever."""
    host = hooks_machine("claude-code", "pi")
    source = host.home / "hook.sh"
    source.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    host.hooks_d.mkdir(parents=True)
    (host.hooks_d / "owned.toml").write_text(
        f'[[hook]]\nname = "owned"\nevents = ["after_tool"]\n'
        f'source = "{source}"\nmatchers = ["Write"]\n',
        encoding="utf-8",
    )

    lifecycle.cmd_install(_args(keep_runtime=True, purge_config=False), host)
    assert live_slots("hooks", home=host.home, env={})

    lifecycle.cmd_uninstall(_args(keep_runtime=True, purge_config=False), host)
    capsys.readouterr()

    assert live_slots("hooks", home=host.home, env={}) == ()


def test_reinstall_closes_the_slot_of_an_event_no_hook_declares_any_more(
    hooks_machine, capsys
):
    """Converging the stanzas down is not closing their slots. An event whose
    last hook was deleted loses its stanza on the next install; the ledger
    record it leaves behind reads as a stanza `verify` can no longer find."""
    host = hooks_machine("claude-code")
    source = host.home / "hook.sh"
    source.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    host.hooks_d.mkdir(parents=True)
    manifest = host.hooks_d / "closing.toml"
    manifest.write_text(
        f'[[hook]]\nname = "closing"\nevents = ["session_end"]\n'
        f'source = "{source}"\n',
        encoding="utf-8",
    )
    (host.hooks_d / "staying.toml").write_text(
        f'[[hook]]\nname = "staying"\nevents = ["after_tool"]\n'
        f'source = "{source}"\nmatchers = ["Write"]\n',
        encoding="utf-8",
    )
    lifecycle.cmd_install(_args(keep_runtime=True, purge_config=False), host)
    assert any("/SessionEnd/" in r.slot
               for r in live_slots("hooks", home=host.home, env={}))

    manifest.unlink()
    lifecycle.cmd_install(_args(keep_runtime=True, purge_config=False), host)
    capsys.readouterr()

    slots = [r.slot for r in live_slots("hooks", home=host.home, env={})]
    assert not any("/SessionEnd/" in slot for slot in slots)
    assert any("/PostToolUse/" in slot for slot in slots)
    assert lifecycle.cmd_verify(_args(), host) == 0


def test_uninstall_takes_the_lock_sidecar_with_it(hooks_machine, capsys):
    host = hooks_machine("claude-code")

    lifecycle.cmd_install(_args(), host)
    lock = host.cc_settings.with_name(host.cc_settings.name + ".nxt.lock")
    assert lock.exists()

    lifecycle.cmd_uninstall(_args(), host)
    capsys.readouterr()
    assert not lock.exists()


# ------------------------------------------- a hook that could not run, said so


def test_a_missing_source_is_not_reported_as_having_run(tmp_path):
    """Open failure for a hook that ran and errored is a deliberate choice: a
    crashing guard must not brick the session. Open failure for a source that
    is not on the machine is a different claim, and it used to be filed in the
    same list, so `doctor` printed a guard's name among the guards that ran.
    """
    entry = {"name": "secret-scan", "events": ["before_tool"],
             "source": str(tmp_path / "not-here.sh"), "policy": "enforce",
             "fail": "open"}

    result = run_hook(entry, {"harness": "claude-code", "event": "before_tool",
                              "cwd": str(tmp_path), "tool": "Write"})
    outcome = aggregate([result])

    assert result["ran"] is False
    assert outcome["ran"] == []
    assert [r["hook"] for r in outcome["unavailable"]] == ["secret-scan"]
    # The verdict is unchanged. Only the claim about execution is.
    assert outcome["verdict"] == "allow"
    assert "source missing" in outcome["context"][0]


def test_a_hook_that_ran_and_failed_is_still_reported_as_having_run(tmp_path):
    source = tmp_path / "boom.py"
    source.write_text("import sys\nsys.exit(3)\n")
    entry = {"name": "boom", "events": ["before_tool"], "source": str(source),
             "policy": "enforce", "fail": "open"}

    outcome = aggregate([run_hook(entry, {"harness": "claude-code",
                                          "event": "before_tool",
                                          "cwd": str(tmp_path), "tool": "Write"})])

    assert [r["hook"] for r in outcome["ran"]] == ["boom"]
    assert outcome["unavailable"] == []


def test_a_fail_closed_hook_with_no_source_still_blocks(tmp_path):
    entry = {"name": "guard", "events": ["before_tool"],
             "source": str(tmp_path / "gone.sh"), "policy": "enforce",
             "fail": "closed"}

    outcome = aggregate([run_hook(entry, {"harness": "claude-code",
                                          "event": "before_tool",
                                          "cwd": str(tmp_path), "tool": "Write"})])

    assert outcome["verdict"] == "block"
    assert outcome["ran"] == []


def test_doctor_reports_a_hook_whose_source_is_absent(hooks_machine, capsys):
    host = hooks_machine("claude-code")
    lifecycle.cmd_install(_args(), host)
    host.hooks_d.mkdir(parents=True, exist_ok=True)
    (host.hooks_d / "ghost.toml").write_text(
        '[[hook]]\nname = "ghost"\nevents = ["before_tool"]\n'
        f'source = "{host.home}/nowhere/ghost.sh"\npolicy = "enforce"\n'
        'fail = "open"\n'
    )
    capsys.readouterr()

    code = lifecycle.cmd_doctor(_args(), host)
    out = capsys.readouterr().out

    assert code == 1
    assert "ghost did not run" in out
    assert "[ok  ] before_tool -> allow  [ghost]" not in out


# ------------------------------------------------------------- fail-quiet


@pytest.mark.parametrize("name", ["cc-dispatch.sh", "codex-dispatch.sh"])
def test_the_installed_transport_is_fail_quiet(hooks_machine, name):
    """A projection can outlive its runtime. The transport exits 0 and says
    so once an hour, rather than turning every turn into a hook error."""
    host = hooks_machine()
    root = lifecycle.install_runtime(host, {"install": {"root": str(host.home / "rt")}})

    text = (root / "bin" / name).read_text()
    assert "if [ ! -f \"$RUNNER\" ]" in text
    # The remedy names this package's own command: a machine with NxtHooks
    # alone has no `nxtos` to run.
    assert "Run: nxthooks doctor" in text and "nxtos doctor" not in text
    assert "-mmin +60" in text
    assert "{{STATE}}" not in text


def _refusing_manifest(name: str) -> str:
    """A manifest `_validate` refuses: `matchers` are tool-name strings."""
    return (f'[[hook]]\nname = "{name}"\nevents = ["before_tool"]\n'
            'source = "/x.py"\nmatchers = ["Write", 42]\n')


def test_a_manifest_refused_at_load_is_a_gap_in_verify(hooks_machine, capsys):
    # T2290, review 856094e0: a refused manifest drops its hook from every
    # wiring check, so without this line verify read healthy with a guard off.
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    host.hooks_d.mkdir(parents=True, exist_ok=True)
    (host.hooks_d / "stray.toml").write_text(_refusing_manifest("stray"))
    capsys.readouterr()

    assert lifecycle.cmd_verify(_args(), host) == 1
    out = capsys.readouterr().out
    assert "[GAP ] manifest refused" in out and "matchers" in out


def test_a_refused_manifest_is_said_once_and_names_the_file_to_fix(
    hooks_machine, capsys
):
    """The report told the operator a manifest was refused and to `run install
    to repair`, which is the wrong verb for a manifest `install` never
    overwrites, and the refusal reached stderr twice because two walks each
    said it (T2337, review d0cf8532).
    """
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    host.hooks_d.mkdir(parents=True, exist_ok=True)
    broken = host.hooks_d / "stray.toml"
    broken.write_text(_refusing_manifest("stray"))
    capsys.readouterr()

    assert lifecycle.cmd_verify(_args(), host) == 1
    captured = capsys.readouterr()

    assert captured.out.count("[GAP ] manifest refused") == 1
    assert f"fix or delete {broken}" in captured.out


def test_a_refusal_in_another_tier_is_a_gap_in_verify(hooks_machine, capsys):
    """A library manifest that will not load is a guard that runs nowhere, and
    `verify`/`doctor` are the verbs that answer for that: only `install`
    declines to fail over a tier it never writes (T2337, review d0cf8532).
    """
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    theirs = host.library_root / "hooks.d"
    theirs.mkdir(parents=True, exist_ok=True)
    (theirs / "theirs.toml").write_text(_refusing_manifest("theirs"))
    capsys.readouterr()

    assert lifecycle.cmd_verify(_args(), host) == 1
    out = capsys.readouterr().out

    assert "[GAP ] manifest refused  [library]" in out
    assert f"fix or delete {theirs / 'theirs.toml'}" in out
    assert "GAPS FOUND" in out


def test_install_succeeds_though_another_components_manifest_is_broken(
    hooks_machine, capsys
):
    """The loop this closes: a broken manifest in someone else's `hooks.d` made
    `install` exit 1, its own final `verify` said so, and the advice was to run
    `install` -- the one verb that cannot write the file it names. The refusal
    is still said, with its remedy, and counted in the summary.
    """
    host = hooks_machine("claude-code")
    theirs = host.library_root / "hooks.d"
    theirs.mkdir(parents=True)
    (theirs / "theirs.toml").write_text(_refusing_manifest("theirs"))

    assert lifecycle.cmd_install(_args(), host) == 0
    out = capsys.readouterr().out

    assert "[warn] manifest refused  [library]" in out
    assert "GAPS FOUND" not in out
    assert "1 manifest refused outside this install's tier" in out


def test_a_refusal_in_the_seeded_tier_fails_install_and_verify(
    hooks_machine, capsys
):
    """The other half of the policy: `hooks.d` is the directory `install`
    seeds, so a manifest it cannot load is this component's own gap and no
    amount of `warn` softens it.
    """
    host = hooks_machine("claude-code")
    host.hooks_d.mkdir(parents=True)
    (host.hooks_d / "stray.toml").write_text(_refusing_manifest("stray"))

    assert lifecycle.cmd_install(_args(), host) == 1
    out = capsys.readouterr().out

    assert "[GAP ] manifest refused  [user]" in out
    assert "GAPS FOUND" in out
    assert lifecycle.cmd_verify(_args(), host) == 1


def test_a_manifest_that_still_loads_a_hook_is_told_to_fix_not_delete(
    hooks_machine, capsys
):
    """A refused stanza in a file that still loads another one is no reason to
    delete that file -- it is running a guard right now (review e097e80b,
    T2337).
    """
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    host.hooks_d.mkdir(parents=True, exist_ok=True)
    mixed = host.hooks_d / "mixed.toml"
    mixed.write_text(
        '[[hook]]\nname = "live"\nevents = ["before_tool"]\n'
        'source = "/live.sh"\nmatchers = ["Write"]\n'
        '[[hook]]\nname = "bad"\nevents = ["before_tool"]\n'
        'source = "/x.py"\nmatchers = ["Write", 42]\n'
    )
    capsys.readouterr()

    assert lifecycle.cmd_verify(_args(), host) == 1
    out = capsys.readouterr().out

    assert out.count("[GAP ] manifest refused") == 1
    assert f"fix the refused entries in {mixed}" in out
    assert "these still run: live" in out
    assert "fix or delete" not in out


def test_a_partly_valid_library_manifest_keeps_who_owns_the_file(
    hooks_machine, capsys
):
    """The fix-not-delete line replaced the whole remedy, so a library file
    that still ran one guard lost the half of the sentence that says `install`
    never writes it -- and went back to being repaired by the one command that
    cannot (T2359).
    """
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    theirs = host.library_root / "hooks.d"
    theirs.mkdir(parents=True)
    (theirs / "theirs.toml").write_text(
        '[[hook]]\nname = "theirs-live"\nevents = ["before_tool"]\n'
        'source = "/theirs.sh"\nmatchers = ["Write"]\n'
        '[[hook]]\nname = "theirs-bad"\nevents = ["before_tool"]\n'
        'source = "/x.py"\nmatchers = ["Write", 42]\n'
    )
    capsys.readouterr()

    assert lifecycle.cmd_verify(_args(), host) == 1
    out = capsys.readouterr().out

    assert "these still run: theirs-live" in out
    assert "the library tier belongs to whichever component installed it" in out
    assert f"fix or delete {theirs / 'theirs.toml'}" not in out


def test_a_partly_valid_builtin_manifest_still_says_reinstall(
    hooks_machine, capsys, monkeypatch, tmp_path
):
    """The builtin tier ships inside the package, so reinstalling was always
    its remedy -- and the fix-not-delete line replaced that half, leaving the
    operator to hand-edit a file the next release overwrites (T2359)."""
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    source = host.home / "shipped.sh"
    source.write_text("#!/bin/sh\n", encoding="utf-8")
    (builtin / "shipped.toml").write_text(
        f'[[hook]]\nname = "shipped-live"\nevents = ["before_tool"]\n'
        f'source = "{source}"\nmatchers = ["Write"]\n'
        '[[hook]]\nname = "shipped-bad"\nevents = ["not_an_event"]\n'
        'source = "/x.py"\n'
    )
    monkeypatch.setattr(registry, "BUILTIN_DIR", builtin)
    capsys.readouterr()

    assert lifecycle.cmd_verify(_args(), host) == 1
    out = capsys.readouterr().out

    assert "these still run: shipped-live" in out
    assert ("it ships inside this package; reinstall NxtHooks to replace it"
            in out)


def test_a_refused_manifest_whose_hooks_are_off_is_not_reported_live(
    hooks_machine, capsys
):
    """The other direction of the same lie: the one hook this user manifest
    still loads switched itself off, so nothing of it runs, and the line that
    reassured the operator that a guard was live named one that never fires.
    """
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    host.hooks_d.mkdir(parents=True, exist_ok=True)
    mixed = host.hooks_d / "quiet.toml"
    mixed.write_text(
        '[[hook]]\nname = "muted"\nevents = ["before_tool"]\n'
        'source = "/muted.sh"\nenabled = false\n'
        '[[hook]]\nname = "bad"\nevents = ["before_tool"]\n'
        'source = "/x.py"\nmatchers = ["Write", 42]\n'
    )
    capsys.readouterr()

    assert lifecycle.cmd_verify(_args(), host) == 1
    out = capsys.readouterr().out

    assert "these still run" not in out
    assert f"fix or delete {mixed}" in out


def test_refusal_report_uses_the_project_default_and_keeps_explicit_policy(
    hooks_machine, capsys, monkeypatch, tmp_path
):
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    project = tmp_path / "project"
    (project / ".nxt").mkdir(parents=True)
    (project / ".nxt" / "hooks.toml").write_text('[policy]\ndefault = "off"\n')
    monkeypatch.chdir(project)
    inherited = host.hooks_d / "inherited.toml"
    inherited.write_text(
        '[[hook]]\nname = "inherited"\nevents = ["before_tool"]\n'
        'source = "/inherited.sh"\n'
        + _refusing_manifest("bad-inherited")
    )
    override = host.hooks_d / "override.toml"
    override.write_text(
        '[[hook]]\nname = "override"\nevents = ["before_tool"]\n'
        'source = "/override.sh"\npolicy = "enforce"\n'
        + _refusing_manifest("bad-override")
    )
    capsys.readouterr()

    assert lifecycle.cmd_verify(_args(), host) == 1
    out = capsys.readouterr().out

    assert f"fix the refused entries in {inherited}\n" in out
    assert f"fix or delete {inherited}" not in out
    assert "these still run: inherited" not in out
    assert "these still run: override" in out


def test_refusal_report_requires_at_least_one_enabled_known_harness(
    hooks_machine, capsys
):
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    mixed = host.hooks_d / "mixed.toml"
    mixed.write_text(
        '[[hook]]\nname = "opted-out"\nevents = ["before_tool"]\n'
        'source = "/opted-out.sh"\n'
        + "".join(f'[hook.harnesses.{h}]\nenabled = false\n' for h in HARNESSES)
        + '[[hook]]\nname = "muse-only"\nevents = ["before_tool"]\n'
        'source = "/muse-only.sh"\n'
        + "".join(f'[hook.harnesses.{h}]\nenabled = false\n' for h in HARNESSES
                  if h != "muse")
        + _refusing_manifest("bad")
    )
    capsys.readouterr()

    assert lifecycle.cmd_verify(_args(), host) == 1
    out = capsys.readouterr().out

    assert "these still run: muse-only\n" in out


def test_the_gap_summary_names_the_refused_manifest_instead_of_install(
    hooks_machine, capsys
):
    """`install` seeds manifests and never overwrites one, so a summary that
    orders it at a refused manifest sends the operator round the loop the
    exit-code policy exists to close (review e097e80b, T2337).
    """
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    broken = host.hooks_d / "stray.toml"
    broken.write_text(_refusing_manifest("stray"))
    capsys.readouterr()

    assert lifecycle.cmd_verify(_args(), host) == 1
    summary = [line for line in capsys.readouterr().out.splitlines()
               if "GAPS FOUND" in line]

    assert len(summary) == 1
    assert str(broken) in summary[0]
    assert "install" not in summary[0]


def test_the_gap_summary_still_offers_install_for_what_install_repairs(
    hooks_machine, capsys
):
    """Both remedies at once, each named: the transport `install` does write is
    still an instruction to run it, beside the manifest it cannot.
    """
    host = hooks_machine("claude-code")
    assert lifecycle.cmd_install(_args(), host) == 0
    (host.root() / "bin" / "cc-dispatch.sh").unlink()
    broken = host.hooks_d / "stray.toml"
    broken.write_text(_refusing_manifest("stray"))
    capsys.readouterr()

    assert lifecycle.cmd_verify(_args(), host) == 1
    summary = [line for line in capsys.readouterr().out.splitlines()
               if "GAPS FOUND" in line]

    assert len(summary) == 1
    assert str(broken) in summary[0] and "install" in summary[0]
