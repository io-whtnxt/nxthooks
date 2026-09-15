"""Test-wide isolation for NxtOS's own roots.

`nxthooks._vendor.paths` resolves to real per-user locations (XDG state, config and
data under the developer's home). Every test that builds a server or a
TrustStore against a tmp_path workspace would otherwise write session
transcripts, a notification database and trust grants into the developer's
actual state directory, and would read back whatever a previous test left
there. Giving the process a tmp home makes the isolation the same as
tmp_path's.

The second fixture is the one that matters since N1. Resolving a path now
*moves* whatever the operator has at the matching legacy location onto it, so
a test that resolves against the real home does not merely read the
developer's state, it migrates it. That is not a thing a test suite may do,
and "it didn't happen this run" is not an assurance -- so it is refused
rather than avoided, naming the path, and a test that trips it is a test that
forgot to inject a home.
"""
import os
from pathlib import Path

import pytest

from nxthooks._vendor import paths

_REAL_HOME = Path.home()


@pytest.fixture(autouse=True)
def _isolated_nxtos_state(tmp_path_factory, monkeypatch):
    # `Path.home`, not the HOME variable, because a resolver reached through
    # the app server (`load_agents`, `scan`, `properties`) is handed
    # `Path.home()` by its caller rather than a test's tmp_path, and since N1
    # resolving one of those *moves* what it finds. Patching the variable
    # instead would also move this interpreter's user site-packages, which
    # every subprocess test then fails to import out of.
    fake_home = tmp_path_factory.mktemp("home")
    developers_home = os.environ.get("HOME")

    def home(cls):
        # A test that sets HOME itself still gets the home it set: several
        # drive a CLI entrypoint that way. Everything else gets the tmp one.
        current = os.environ.get("HOME")
        return fake_home if current == developers_home else Path(current)

    monkeypatch.setattr(Path, "home", classmethod(home))
    # Deliberately not an `NXT_*_HOME` variable. Those replace the whole root
    # and outrank the `home` a caller passes, so setting one here would make
    # every test that injects a tmp home resolve somewhere else instead.
    for key in ("NXTOS_STATE_HOME", "NXT_CONFIG_HOME", "NXT_DATA_HOME",
                "NXT_STATE_HOME", "NXT_CACHE_HOME", "XDG_CONFIG_HOME",
                "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                # A harness's own home override outranks the `home` a caller
                # passes, exactly like the NXT_* ones. An operator with
                # CLAUDE_CONFIG_DIR set would otherwise have every projection
                # test aimed at their real ~/.claude.
                "CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME"):
        monkeypatch.delenv(key, raising=False)
    # Both adoption passes run at most once per (source, target) per process.
    # Two tests can reuse a workspace path, so the caches are cleared here or
    # the second one silently skips the adoption it is asserting on.
    paths._adopted.clear()


@pytest.fixture(autouse=True)
def _never_adopt_the_developers_own_state(monkeypatch):
    real_adopt = paths.adopt

    def guarded(legacy, target, *args, **kwargs):
        for candidate in (Path(legacy), Path(target)):
            if candidate == _REAL_HOME or _REAL_HOME in candidate.parents:
                raise AssertionError(
                    f"a test tried to migrate {candidate}, which is under the "
                    "developer's real home; inject a home into the resolver "
                    "instead of letting it default to Path.home()"
                )
        return real_adopt(legacy, target, *args, **kwargs)

    monkeypatch.setattr(paths, "adopt", guarded)


# ---------------------------------------------------------------------------
# NxtHooks: a throwaway machine to project into
# ---------------------------------------------------------------------------

#: One file per harness that the harness itself writes. A *directory* is never
#: a sentinel: `~/.pi/agent/extensions` and `~/.config/opencode/plugins` are
#: directories NxtHooks creates, so seeding one would let a test detect the
#: harness NxtHooks just manufactured.
HARNESS_SENTINELS = {
    "claude-code": (".claude/settings.json", "{}\n"),
    "codex": (".codex/config.toml", 'model = "gpt-5"\n'),
    "pi": (".pi/settings.json", "{}\n"),
    "opencode": (".config/opencode/opencode.json", "{}\n"),
    "hermes": (".hermes/config.yaml", "model:\n  name: local\n"),
}


@pytest.fixture
def hooks_machine(tmp_path):
    """Build a `nxthooks.lifecycle.Host` on an empty simulated machine.

    `env` is empty and `path_lookup` answers nothing, so neither the
    developer's XDG variables nor the fact that they have `claude` on PATH can
    decide what a test observes. Presence is exactly what the test seeds.
    """
    from nxthooks import lifecycle

    def build(*present: str) -> "lifecycle.Host":
        home = tmp_path / "machine"
        home.mkdir(exist_ok=True)
        for name in present:
            relative, body = HARNESS_SENTINELS[name]
            target = home / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        return lifecycle.Host(home=home, env={}, path_lookup=lambda name: None)

    return build
