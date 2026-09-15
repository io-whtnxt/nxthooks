"""Which install roots an uninstall is allowed to strip, and where it learns
them from.

The `installed-roots` file this package used to write is superseded by the
projection ledger: every wired stanza records the dispatcher it points at, so
the roots fall out of `runtime`. The file is still *read*, because a machine
installed before the ledger existed has one and a stanza it names is a stanza
that would otherwise outlive the script it runs.
"""

from __future__ import annotations

import json

from nxthooks import lifecycle


class _Args:
    harnesses = None
    keep_runtime = True
    purge_config = False


def test_uninstall_with_no_history_at_all_strips_by_marker_alone(hooks_machine):
    """The pre-hardening machine: a stanza wired by root A, no ledger, no
    `installed-roots`, and uninstall running with root B configured. No
    marker-carrying stanza may survive pointing at a script the uninstall is
    about to delete.
    """
    host = hooks_machine("claude-code", "codex")
    root_a = host.home / "root-a"
    for path, wired in ((host.cc_settings, "cc-dispatch.sh"),
                        (host.codex_hooks, "codex-dispatch.sh")):
        path.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{
            "type": "command",
            "command": f'{lifecycle.MARKER} sh "{root_a}/bin/{wired}" before_tool',
        }]}]}}))

    assert lifecycle._known_roots(host) is None

    root_b = host.home / "root-b"
    for line in lifecycle.cc_unwire(host, None):
        assert line
    lifecycle.codex_unwire(host, None)

    assert json.loads(host.cc_settings.read_text()) == {}
    assert json.loads(host.codex_hooks.read_text()) == {}
    assert not root_b.exists()


def test_a_known_root_narrows_the_strip_and_spares_a_foreign_install(hooks_machine):
    """With history, the strip is root-scoped, so a genuinely foreign
    NxtHooks install's stanzas are left alone."""
    host = hooks_machine("claude-code")
    other = {
        "type": "command",
        "command": f'{lifecycle.MARKER} sh "/other/runtime/bin/cc-dispatch.sh" before_tool',
    }
    host.cc_settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [other]}]}})
    )
    root = host.home / "runtime"
    lifecycle.cc_wire(host, root, {"before_tool": [None]})

    assert lifecycle._known_roots(host) == (root,)

    lifecycle.cc_unwire(host, lifecycle._known_roots(host))
    data = json.loads(host.cc_settings.read_text())
    assert other in data["hooks"]["PreToolUse"][0]["hooks"]


def test_the_roots_come_out_of_the_ledgers_runtime_field(hooks_machine):
    host = hooks_machine("claude-code", "codex")
    root = host.home / "runtime"
    lifecycle.cc_wire(host, root, {"before_tool": [None]})
    lifecycle.codex_wire(host, root)

    assert lifecycle._known_roots(host) == (root,)


def test_a_legacy_installed_roots_file_is_still_read(hooks_machine):
    """Superseded, not ignored. A machine that predates the ledger has this
    file and nothing else, and its roots are the only record of what was
    wired."""
    host = hooks_machine("claude-code")
    old_root = host.home / "old-runtime"
    host.config_dir.mkdir(parents=True)
    (host.config_dir / "installed-roots").write_text(f"{old_root}\n\n")

    assert lifecycle._known_roots(host) == (old_root,)


def test_a_recorded_root_is_the_unresolved_one_the_stanza_embeds(hooks_machine):
    """A wired stanza embeds the root exactly as configured. Resolving it
    here, through a symlinked install root, would silently stop matching the
    stanzas it is meant to find."""
    host = hooks_machine("claude-code")
    real = host.home / "real-runtime"
    real.mkdir()
    link = host.home / "symlinked-runtime"
    link.symlink_to(real)

    lifecycle.cc_wire(host, link, {"before_tool": [None]})
    roots = lifecycle._known_roots(host)

    assert roots == (link,)
    assert roots != (link.resolve(),)
